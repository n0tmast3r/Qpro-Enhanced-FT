using System.Buffers.Binary;
using System.IO.MemoryMappedFiles;
using System.Net;
using System.Net.Sockets;
using System.Runtime.Versioning;
using Microsoft.Extensions.Logging;
using VRCFaceTracking;
using VRCFaceTracking.Core.Params.Expressions;

namespace Qpro.GazeBridge;

[SupportedOSPlatform("windows")]

public sealed class TrackingModule : ExtTrackingModule
{
    private const string MapName = "VirtualDesktop.BodyState";
    private const int StateBytes = 360;
    private const int ExpressionOffset = 4;
    private const int ExpressionCount = 70;
    private const int GazePort = 27275;
    private const int GazePacketBytes = 24;
    private const long GazeTimeoutMs = 250;
    private const int TonguePort = 27276;
    private const int TonguePacketBytes = 56;
    private const long TongueTimeoutMs = 300;
    private static readonly (int Source, int[] Targets)[] ExpressionMap =
    [
        (2, [19]), (3, [18]), (4, [17]), (5, [16]),
        (6, [21]), (7, [20]), (8, [67]), (9, [66]),
        (10, [65]), (11, [64]), (24, [22]), (25, [24]),
        (26, [23]), (27, [25]), (30, [61]), (31, [60]),
        (32, [57, 59]), (33, [56, 58]), (34, [39]), (35, [37]),
        (36, [38]), (37, [36]), (38, [69]), (39, [68]),
        (40, [41, 43]), (41, [40, 42]), (42, [63]), (43, [62]),
        (44, [33]), (46, [32]), (48, [71]), (49, [70]),
        (50, [29]), (51, [51]), (52, [50]), (53, [53, 55]),
        (54, [52, 54]), (55, [49]), (56, [48]),
        (61, [45, 47]), (62, [44, 46])
    ];

    private readonly byte[] _first = new byte[StateBytes];
    private readonly byte[] _second = new byte[StateBytes];
    private MemoryMappedFile? _map;
    private MemoryMappedViewAccessor? _view;
    private UdpClient? _gazeSocket;
    private UdpClient? _tongueSocket;
    private bool _needsEye;
    private bool _needsExpression;
    private long _lastGazeTick;
    private float _leftGazeX;
    private float _leftGazeY;
    private float _rightGazeX;
    private float _rightGazeY;
    private byte _gazeFlags;
    private readonly float[] _tongueValues = new float[12];
    private bool _tongueEnabled;
    private bool _tongueDirty;
    private bool _customTongueWasApplied;
    private long _lastTongueTick;

    // This is a complete Virtual Desktop reader so one module owns both VRCFT
    // slots. It preserves the stock blink/face mapping and substitutes only
    // fresh independent gaze packets.
    public override (bool SupportsEye, bool SupportsExpression) Supported => (true, true);

    public override (bool eyeSuccess, bool expressionSuccess) Initialize(
        bool eyeAvailable,
        bool expressionAvailable)
    {
        _needsEye = eyeAvailable;
        _needsExpression = expressionAvailable;
        ModuleInformation = new ModuleMetadata
        {
            Name = "Quest Pro independent gaze + Virtual Desktop face/tongue v0.6"
        };

        try
        {
            _gazeSocket = new UdpClient(new IPEndPoint(IPAddress.Loopback, GazePort));
            _gazeSocket.Client.Blocking = false;
        }
        catch (SocketException error)
        {
            Logger.LogError(error, "Could not bind the local Quest Pro gaze port {Port}", GazePort);
        }

        try
        {
            _tongueSocket = new UdpClient(new IPEndPoint(IPAddress.Loopback, TonguePort));
            _tongueSocket.Client.Blocking = false;
        }
        catch (SocketException error)
        {
            Logger.LogError(error, "Could not bind the local Quest Pro tongue port {Port}", TonguePort);
        }

        TryOpenMap();
        Logger.LogInformation(
            "Quest Pro full Virtual Desktop bridge initialized (eye={Eye}, face={Face}); " +
            "custom gaze falls back to Virtual Desktop after {Timeout} ms",
            _needsEye, _needsExpression, GazeTimeoutMs);
        return (_needsEye, _needsExpression);
    }

    public override void Update()
    {
        ReceiveGaze();
        ReceiveTongue();
        if (!TryReadState())
        {
            Thread.Sleep(10);
            return;
        }

        Span<float> expressions = stackalloc float[ExpressionCount];
        for (int index = 0; index < ExpressionCount; ++index)
            expressions[index] = BitConverter.ToSingle(_second, ExpressionOffset + index * 4);

        if (_needsEye)
            UpdateEyes(expressions);
        if (_needsExpression && _second[1] != 0)
            UpdateMouth(expressions, _second[1]);
        Thread.Sleep(5);
    }

    public override void Teardown()
    {
        _gazeSocket?.Dispose();
        _gazeSocket = null;
        _tongueSocket?.Dispose();
        _tongueSocket = null;
        _view?.Dispose();
        _view = null;
        _map?.Dispose();
        _map = null;
    }

    private void TryOpenMap()
    {
        if (_view is not null)
            return;
        try
        {
            _map = MemoryMappedFile.OpenExisting(MapName, MemoryMappedFileRights.Read);
            _view = _map.CreateViewAccessor(0, StateBytes, MemoryMappedFileAccess.Read);
        }
        catch (FileNotFoundException)
        {
            _map?.Dispose();
            _map = null;
        }
    }

    private bool TryReadState()
    {
        TryOpenMap();
        if (_view is null)
            return false;
        try
        {
            _view.ReadArray(0, _first, 0, StateBytes);
            Thread.MemoryBarrier();
            _view.ReadArray(0, _second, 0, StateBytes);
            return _first.AsSpan().SequenceEqual(_second);
        }
        catch (ObjectDisposedException)
        {
            _view = null;
            _map = null;
            return false;
        }
    }

    private void ReceiveGaze()
    {
        if (_gazeSocket is null)
            return;
        try
        {
            while (_gazeSocket.Available >= GazePacketBytes)
            {
                IPEndPoint sender = new(IPAddress.Loopback, 0);
                byte[] packet = _gazeSocket.Receive(ref sender);
                if (packet.Length != GazePacketBytes ||
                    packet[0] != (byte)'Q' || packet[1] != (byte)'P' ||
                    packet[2] != (byte)'G' || packet[3] != (byte)'E' ||
                    packet[4] != 1)
                    continue;
                _gazeFlags = packet[5];
                _leftGazeX = ReadFloat(packet, 8);
                _leftGazeY = ReadFloat(packet, 12);
                _rightGazeX = ReadFloat(packet, 16);
                _rightGazeY = ReadFloat(packet, 20);
                _lastGazeTick = Environment.TickCount64;
            }
        }
        catch (SocketException error) when (
            error.SocketErrorCode is SocketError.WouldBlock or SocketError.IOPending)
        {
        }
    }

    private void ReceiveTongue()
    {
        if (_tongueSocket is null)
            return;
        try
        {
            while (_tongueSocket.Available >= TonguePacketBytes)
            {
                IPEndPoint sender = new(IPAddress.Loopback, 0);
                byte[] packet = _tongueSocket.Receive(ref sender);
                if (packet.Length != TonguePacketBytes ||
                    packet[0] != (byte)'Q' || packet[1] != (byte)'P' ||
                    packet[2] != (byte)'T' || packet[3] != (byte)'O' ||
                    packet[4] != 1)
                    continue;
                _tongueEnabled = (packet[5] & 1) != 0;
                for (int index = 0; index < _tongueValues.Length; ++index)
                    _tongueValues[index] = Math.Clamp(ReadFloat(packet, 8 + index * 4), 0.0f, 1.0f);
                _tongueDirty = true;
                _lastTongueTick = Environment.TickCount64;
            }
        }
        catch (SocketException error) when (
            error.SocketErrorCode is SocketError.WouldBlock or SocketError.IOPending)
        {
        }
    }

    private void UpdateEyes(ReadOnlySpan<float> values)
    {
        bool leftValid = _second[292] != 0;
        bool rightValid = _second[293] != 0;
        bool customFresh = _lastGazeTick != 0 &&
            Environment.TickCount64 - _lastGazeTick <= GazeTimeoutMs;

        if (customFresh && (_gazeFlags & 1) != 0)
        {
            UnifiedTracking.Data.Eye.Left.Gaze.x = _leftGazeX;
            UnifiedTracking.Data.Eye.Left.Gaze.y = _leftGazeY;
        }
        else if (leftValid)
        {
            (float x, float y) = QuaternionToCartesian(_second, 296);
            UnifiedTracking.Data.Eye.Left.Gaze.x = x;
            UnifiedTracking.Data.Eye.Left.Gaze.y = y;
        }

        if (customFresh && (_gazeFlags & 2) != 0)
        {
            UnifiedTracking.Data.Eye.Right.Gaze.x = _rightGazeX;
            UnifiedTracking.Data.Eye.Right.Gaze.y = _rightGazeY;
        }
        else if (rightValid)
        {
            (float x, float y) = QuaternionToCartesian(_second, 324);
            UnifiedTracking.Data.Eye.Right.Gaze.x = x;
            UnifiedTracking.Data.Eye.Right.Gaze.y = y;
        }

        UnifiedTracking.Data.Eye.Left.Openness = 1.0f - Math.Clamp(
            values[12] + values[4] * values[28], 0.0f, 1.0f);
        UnifiedTracking.Data.Eye.Right.Openness = 1.0f - Math.Clamp(
            values[13] + values[5] * values[29], 0.0f, 1.0f);
        UnifiedTracking.Data.Eye.Left.PupilDiameter_MM = 5.0f;
        UnifiedTracking.Data.Eye.Right.PupilDiameter_MM = 5.0f;
        UnifiedTracking.Data.Eye._minDilation = 0.0f;
        UnifiedTracking.Data.Eye._maxDilation = 10.0f;

        if (_second[1] != 0)
            UpdateEyeExpressions(values);
    }

    private static void UpdateEyeExpressions(ReadOnlySpan<float> values)
    {
        Set(5, values[0]); Set(7, values[0]);
        Set(4, values[1]); Set(6, values[1]);
        Set(9, values[22]); Set(8, values[23]);
        Set(1, values[28]); Set(0, values[29]);
        Set(11, values[57]); Set(10, values[58]);
        Set(3, values[59]); Set(2, values[60]);
    }

    private void UpdateMouth(ReadOnlySpan<float> values, byte faceFlags)
    {
        // Virtual Desktop mapping derived from the official MIT-licensed module:
        // https://github.com/guygodin/VirtualDesktop.VRCFaceTracking
        foreach ((int source, int[] targets) in ExpressionMap)
            foreach (int target in targets)
                Set(target, values[source]);

        Set(31, Math.Min(1.0f - MathF.Pow(values[61], 1.0f / 6.0f), values[45]));
        Set(30, Math.Min(1.0f - MathF.Pow(values[62], 1.0f / 6.0f), values[47]));

        bool customFresh = _lastTongueTick != 0 &&
            Environment.TickCount64 - _lastTongueTick <= TongueTimeoutMs;
        if (customFresh && _tongueEnabled)
        {
            // Do not rewrite unchanged tongue shapes at the module's ~200 Hz
            // face cadence. A new camera packet is the only thing that should
            // dirty these slots; this avoids flooding VRCFT/VRChat while the
            // rest of the face remains on its normal update path.
            if (_tongueDirty)
            {
                ApplyTongueOverride();
                _tongueDirty = false;
                _customTongueWasApplied = true;
            }
        }
        else
        {
            if (_customTongueWasApplied)
            {
                ClearDetailedTongueOverride();
                _customTongueWasApplied = false;
            }
            if ((faceFlags & 2) != 0)
            {
                Set(72, values[63]);
                Set(76, values[64]);
                Set(75, values[65]);
                Set(73, values[66]);
                Set(74, values[67]);
            }
            else
            {
                Set(72, values[68]);
                Set(79, values[64]);
            }
        }
    }

    private void ApplyTongueOverride()
    {
        Set((int)UnifiedExpressions.TongueOut, _tongueValues[0]);
        // TongueRetreat is an independent expression, not the complement of
        // TongueOut. Neutral is zero for both. The camera model has no retreat
        // head, so leave it neutral rather than forcing a full-strength mouth
        // shape whenever the tongue is hidden.
        Set(79, 0.0f);
        Set((int)UnifiedExpressions.TongueUp, _tongueValues[1]);
        Set((int)UnifiedExpressions.TongueDown, _tongueValues[2]);
        Set((int)UnifiedExpressions.TongueLeft, _tongueValues[3]);
        Set((int)UnifiedExpressions.TongueRight, _tongueValues[4]);
        Set((int)UnifiedExpressions.TongueRoll, _tongueValues[5]);
        Set((int)UnifiedExpressions.TongueBendDown, _tongueValues[6]);
        Set((int)UnifiedExpressions.TongueCurlUp, _tongueValues[7]);
        Set((int)UnifiedExpressions.TongueSquish, _tongueValues[8]);
        Set((int)UnifiedExpressions.TongueFlat, _tongueValues[9]);
        Set((int)UnifiedExpressions.TongueTwistLeft, _tongueValues[10]);
        Set((int)UnifiedExpressions.TongueTwistRight, _tongueValues[11]);
    }

    private static void ClearDetailedTongueOverride()
    {
        Set((int)UnifiedExpressions.TongueOut, 0.0f);
        Set(79, 0.0f);
        Set((int)UnifiedExpressions.TongueUp, 0.0f);
        Set((int)UnifiedExpressions.TongueDown, 0.0f);
        Set((int)UnifiedExpressions.TongueLeft, 0.0f);
        Set((int)UnifiedExpressions.TongueRight, 0.0f);
        Set((int)UnifiedExpressions.TongueRoll, 0.0f);
        Set((int)UnifiedExpressions.TongueBendDown, 0.0f);
        Set((int)UnifiedExpressions.TongueCurlUp, 0.0f);
        Set((int)UnifiedExpressions.TongueSquish, 0.0f);
        Set((int)UnifiedExpressions.TongueFlat, 0.0f);
        Set((int)UnifiedExpressions.TongueTwistLeft, 0.0f);
        Set((int)UnifiedExpressions.TongueTwistRight, 0.0f);
    }

    private static void Set(int expression, float value) =>
        UnifiedTracking.Data.Shapes[expression].Weight = value;

    private static float ReadFloat(byte[] bytes, int offset) =>
        BitConverter.Int32BitsToSingle(BinaryPrimitives.ReadInt32LittleEndian(bytes.AsSpan(offset, 4)));

    private static (float x, float y) QuaternionToCartesian(byte[] bytes, int offset)
    {
        float x = BitConverter.ToSingle(bytes, offset);
        float y = BitConverter.ToSingle(bytes, offset + 4);
        float z = BitConverter.ToSingle(bytes, offset + 8);
        float w = BitConverter.ToSingle(bytes, offset + 12);
        float length = MathF.Sqrt(x * x + y * y + z * z + w * w);
        if (length <= 1e-6f)
            return (0.0f, 0.0f);
        x /= length; y /= length; z /= length; w /= length;
        float first = MathF.Asin(Math.Clamp(2.0f * (x * z - w * y), -1.0f, 1.0f));
        float second = MathF.Atan2(
            2.0f * (y * z + w * x),
            w * w - x * x - y * y + z * z);
        return (first, second);
    }
}
