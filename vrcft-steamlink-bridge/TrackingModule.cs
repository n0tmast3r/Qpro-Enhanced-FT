using System.Buffers.Binary;
using System.IO.MemoryMappedFiles;
using System.Net;
using System.Net.Sockets;
using System.Runtime.Versioning;
using System.Text;
using Microsoft.Extensions.Logging;
using VRCFaceTracking;
using VRCFaceTracking.Core.Params.Expressions;

namespace Qpro.SteamLinkBridge;

[SupportedOSPlatform("windows")]
public sealed class TrackingModule : ExtTrackingModule
{
    private const int SteamLinkPort = 9015;
    private const long SteamLinkTimeoutMs = 500;
    private const int ExpressionCount = 70;
    private const int GazePort = 27275;
    private const int GazePacketBytes = 24;
    private const long GazeTimeoutMs = 250;
    private const int TonguePort = 27276;
    private const int TonguePacketBytes = 56;
    private const long TongueTimeoutMs = 300;
    private const string FaceWeightPrefix = "/sl/xrfb/facew/";
    private const string GazePointAddress = "/sl/eyeTrackedGazePoint";
    // Tongue capture needs a factory reference stream, and this module owns
    // port 9015, so it republishes the weights in Virtual Desktop's BodyState
    // layout (flags byte 0, 70 floats from offset 4) for vd-label-bridge.
    private const string ReferenceMapName = "Qpro.SteamLink.BodyState";
    private const int ReferenceStateBytes = 360;
    private const int ReferenceExpressionOffset = 4;

    // XR_FB_face_tracking2 order, the same indices Virtual Desktop's shared
    // memory uses, so the mapping below is identical to the VD bridge.
    private static readonly string[] WeightNames =
    [
        "BrowLowererL", "BrowLowererR", "CheekPuffL", "CheekPuffR",
        "CheekRaiserL", "CheekRaiserR", "CheekSuckL", "CheekSuckR",
        "ChinRaiserB", "ChinRaiserT", "DimplerL", "DimplerR",
        "EyesClosedL", "EyesClosedR", "EyesLookDownL", "EyesLookDownR",
        "EyesLookLeftL", "EyesLookLeftR", "EyesLookRightL", "EyesLookRightR",
        "EyesLookUpL", "EyesLookUpR", "InnerBrowRaiserL", "InnerBrowRaiserR",
        "JawDrop", "JawSidewaysLeft", "JawSidewaysRight", "JawThrust",
        "LidTightenerL", "LidTightenerR", "LipCornerDepressorL", "LipCornerDepressorR",
        "LipCornerPullerL", "LipCornerPullerR", "LipFunnelerLB", "LipFunnelerLT",
        "LipFunnelerRB", "LipFunnelerRT", "LipPressorL", "LipPressorR",
        "LipPuckerL", "LipPuckerR", "LipStretcherL", "LipStretcherR",
        "LipSuckLB", "LipSuckLT", "LipSuckRB", "LipSuckRT",
        "LipTightenerL", "LipTightenerR", "LipsToward", "LowerLipDepressorL",
        "LowerLipDepressorR", "MouthLeft", "MouthRight", "NoseWrinklerL",
        "NoseWrinklerR", "OuterBrowRaiserL", "OuterBrowRaiserR", "UpperLidRaiserL",
        "UpperLidRaiserR", "UpperLipRaiserL", "UpperLipRaiserR", "TongueTipInterdental",
        "TongueTipAlveolar", "FrontDorsalPalate", "MidDorsalPalate", "BackDorsalVelar",
        "TongueOut", "TongueRetreat"
    ];

    private static readonly Dictionary<string, int> WeightIndex = BuildWeightIndex();

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

    private readonly float[] _weights = new float[ExpressionCount];
    private readonly float[] _gazePoint = new float[3];
    private readonly byte[] _oscBuffer = new byte[65536];
    private Socket? _oscSocket;
    private UdpClient? _gazeSocket;
    private UdpClient? _tongueSocket;
    private bool _needsEye;
    private bool _needsExpression;
    private long _lastFaceTick;
    private long _lastEyeTick;
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
    private MemoryMappedFile? _referenceFile;
    private MemoryMappedViewAccessor? _referenceView;
    private readonly byte[] _referenceState = new byte[ReferenceStateBytes];
    private long _lastPublishedFaceTick;
    private bool _referenceValid;

    // Complete Steam Link reader so one module owns both VRCFT slots. It uses
    // the same stock face mapping as the Virtual Desktop bridge and substitutes
    // only fresh independent gaze and opt-in tongue packets.
    public override (bool SupportsEye, bool SupportsExpression) Supported => (true, true);

    public override (bool eyeSuccess, bool expressionSuccess) Initialize(
        bool eyeAvailable,
        bool expressionAvailable)
    {
        _needsEye = eyeAvailable;
        _needsExpression = expressionAvailable;
        ModuleInformation = new ModuleMetadata
        {
            Name = "Quest Pro independent gaze + Steam Link face/tongue v0.6"
        };

        try
        {
            _oscSocket = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp);
            _oscSocket.Bind(new IPEndPoint(IPAddress.Loopback, SteamLinkPort));
            _oscSocket.Blocking = false;
        }
        catch (SocketException error)
        {
            Logger.LogError(error,
                "Could not bind Steam Link OSC port {Port}. Remove other Steam Link VRCFT modules from CustomLibs.",
                SteamLinkPort);
            _oscSocket?.Dispose();
            _oscSocket = null;
            return (false, false);
        }

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

        try
        {
            _referenceFile = MemoryMappedFile.CreateOrOpen(ReferenceMapName, ReferenceStateBytes);
            _referenceView = _referenceFile.CreateViewAccessor(0, ReferenceStateBytes);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            Logger.LogError(error, "Could not create the tongue capture reference map {Name}", ReferenceMapName);
            _referenceFile?.Dispose();
            _referenceFile = null;
        }

        Logger.LogInformation(
            "Quest Pro full Steam Link bridge initialized (eye={Eye}, face={Face}); " +
            "listening for Steam Link OSC on {Port}, custom gaze falls back to Steam Link after {Timeout} ms",
            _needsEye, _needsExpression, SteamLinkPort, GazeTimeoutMs);
        return (_needsEye, _needsExpression);
    }

    public override void Update()
    {
        ReceiveGaze();
        ReceiveTongue();
        ReceiveSteamLink();

        long now = Environment.TickCount64;
        bool faceFresh = _lastFaceTick != 0 && now - _lastFaceTick <= SteamLinkTimeoutMs;
        bool eyeFresh = _lastEyeTick != 0 && now - _lastEyeTick <= SteamLinkTimeoutMs;
        PublishReference(faceFresh);
        if (!faceFresh && !eyeFresh && !CustomGazeFresh(now))
        {
            Thread.Sleep(10);
            return;
        }

        if (_needsEye)
            UpdateEyes(eyeFresh, faceFresh);
        if (_needsExpression && faceFresh)
            UpdateMouth(_weights);
        Thread.Sleep(5);
    }

    public override void Teardown()
    {
        _oscSocket?.Dispose();
        _oscSocket = null;
        _gazeSocket?.Dispose();
        _gazeSocket = null;
        _tongueSocket?.Dispose();
        _tongueSocket = null;
        _referenceView?.Dispose();
        _referenceView = null;
        _referenceFile?.Dispose();
        _referenceFile = null;
    }

    // Writes only when new OSC weights arrived (or once when they go stale),
    // so the label bridge's unchanged-time check still sees a stopped source.
    private void PublishReference(bool faceFresh)
    {
        if (_referenceView is null)
            return;
        if (faceFresh ? _lastFaceTick == _lastPublishedFaceTick : !_referenceValid)
            return;
        _referenceState[0] = faceFresh ? (byte)1 : (byte)0;
        for (int index = 0; index < ExpressionCount; ++index)
            BinaryPrimitives.WriteSingleLittleEndian(
                _referenceState.AsSpan(ReferenceExpressionOffset + index * 4, 4), _weights[index]);
        _referenceView.WriteArray(0, _referenceState, 0, ReferenceStateBytes);
        _lastPublishedFaceTick = _lastFaceTick;
        _referenceValid = faceFresh;
    }

    private static Dictionary<string, int> BuildWeightIndex()
    {
        Dictionary<string, int> index = new(StringComparer.Ordinal);
        for (int i = 0; i < WeightNames.Length; ++i)
            index[FaceWeightPrefix + WeightNames[i]] = i;
        return index;
    }

    private void ReceiveSteamLink()
    {
        if (_oscSocket is null)
            return;
        try
        {
            while (_oscSocket.Available > 0)
            {
                int length = _oscSocket.Receive(_oscBuffer);
                ParsePacket(_oscBuffer.AsSpan(0, length), 0);
            }
        }
        catch (SocketException error) when (
            error.SocketErrorCode is SocketError.WouldBlock or SocketError.IOPending
                or SocketError.ConnectionReset or SocketError.MessageSize)
        {
        }
    }

    private void ParsePacket(ReadOnlySpan<byte> data, int depth)
    {
        if (depth > 8)
            return;
        if (data.Length >= 16 && data[..8].SequenceEqual("#bundle\0"u8))
        {
            int position = 16;
            while (position + 4 <= data.Length)
            {
                int size = BinaryPrimitives.ReadInt32BigEndian(data.Slice(position, 4));
                position += 4;
                if (size <= 0 || size > data.Length - position)
                    return;
                ParsePacket(data.Slice(position, size), depth + 1);
                position += size;
            }
            return;
        }
        ParseMessage(data);
    }

    private void ParseMessage(ReadOnlySpan<byte> data)
    {
        int addressEnd = data.IndexOf((byte)0);
        if (addressEnd <= 0 || data[0] != (byte)'/')
            return;
        int position = Align4(addressEnd + 1);
        if (position >= data.Length || data[position] != (byte)',')
            return;
        int tagsEnd = data[position..].IndexOf((byte)0);
        if (tagsEnd < 0)
            return;
        ReadOnlySpan<byte> tags = data.Slice(position + 1, tagsEnd - 1);
        position = Align4(position + tagsEnd + 1);

        Span<float> floats = stackalloc float[3];
        int floatCount = 0;
        foreach (byte tag in tags)
        {
            switch (tag)
            {
                case (byte)'f':
                    if (position + 4 > data.Length)
                        return;
                    if (floatCount < floats.Length)
                        floats[floatCount++] = BinaryPrimitives.ReadSingleBigEndian(data.Slice(position, 4));
                    position += 4;
                    break;
                case (byte)'i':
                case (byte)'c':
                case (byte)'r':
                case (byte)'m':
                    position += 4;
                    break;
                case (byte)'d':
                case (byte)'h':
                case (byte)'t':
                    position += 8;
                    break;
                case (byte)'s':
                case (byte)'S':
                    int stringEnd = position < data.Length ? data[position..].IndexOf((byte)0) : -1;
                    if (stringEnd < 0)
                        return;
                    position = Align4(position + stringEnd + 1);
                    break;
                case (byte)'b':
                    if (position + 4 > data.Length)
                        return;
                    position = Align4(position + 4 + BinaryPrimitives.ReadInt32BigEndian(data.Slice(position, 4)));
                    break;
                case (byte)'T':
                case (byte)'F':
                case (byte)'N':
                case (byte)'I':
                    break;
                default:
                    return;
            }
        }
        if (floatCount == 0)
            return;

        string address = Encoding.ASCII.GetString(data[..addressEnd]);
        if (WeightIndex.TryGetValue(address, out int weight))
        {
            _weights[weight] = floats[0];
            _lastFaceTick = Environment.TickCount64;
        }
        else if (floatCount >= 3 && address == GazePointAddress)
        {
            _gazePoint[0] = floats[0];
            _gazePoint[1] = floats[1];
            _gazePoint[2] = floats[2];
            _lastEyeTick = Environment.TickCount64;
        }
    }

    private static int Align4(int value) => (value + 3) & ~3;

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

    private bool CustomGazeFresh(long now) =>
        _lastGazeTick != 0 && now - _lastGazeTick <= GazeTimeoutMs;

    private void UpdateEyes(bool eyeFresh, bool faceFresh)
    {
        bool customFresh = CustomGazeFresh(Environment.TickCount64);

        // Steam Link only sends one combined gaze point, so the stock fallback
        // drives both eyes with the same direction.
        float stockX = 0.0f;
        float stockY = 0.0f;
        if (eyeFresh)
            (stockX, stockY) = GazePointToAngles(_gazePoint);

        if (customFresh && (_gazeFlags & 1) != 0)
        {
            UnifiedTracking.Data.Eye.Left.Gaze.x = _leftGazeX;
            UnifiedTracking.Data.Eye.Left.Gaze.y = _leftGazeY;
        }
        else if (eyeFresh)
        {
            UnifiedTracking.Data.Eye.Left.Gaze.x = stockX;
            UnifiedTracking.Data.Eye.Left.Gaze.y = stockY;
        }

        if (customFresh && (_gazeFlags & 2) != 0)
        {
            UnifiedTracking.Data.Eye.Right.Gaze.x = _rightGazeX;
            UnifiedTracking.Data.Eye.Right.Gaze.y = _rightGazeY;
        }
        else if (eyeFresh)
        {
            UnifiedTracking.Data.Eye.Right.Gaze.x = stockX;
            UnifiedTracking.Data.Eye.Right.Gaze.y = stockY;
        }

        UnifiedTracking.Data.Eye.Left.PupilDiameter_MM = 5.0f;
        UnifiedTracking.Data.Eye.Right.PupilDiameter_MM = 5.0f;
        UnifiedTracking.Data.Eye._minDilation = 0.0f;
        UnifiedTracking.Data.Eye._maxDilation = 10.0f;

        if (!faceFresh)
            return;

        float[] values = _weights;
        UnifiedTracking.Data.Eye.Left.Openness = 1.0f - Math.Clamp(
            values[12] + values[12] * values[28], 0.0f, 1.0f);
        UnifiedTracking.Data.Eye.Right.Openness = 1.0f - Math.Clamp(
            values[13] + values[13] * values[29], 0.0f, 1.0f);
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

    private void UpdateMouth(ReadOnlySpan<float> values)
    {
        // Same mapping as the Virtual Desktop bridge, derived from the official
        // MIT-licensed module: https://github.com/guygodin/VirtualDesktop.VRCFaceTracking
        foreach ((int source, int[] targets) in ExpressionMap)
            foreach (int target in targets)
                Set(target, values[source]);

        Set(31, Math.Min(1.0f - MathF.Pow(values[61], 1.0f / 6.0f), values[45]));
        Set(30, Math.Min(1.0f - MathF.Pow(values[62], 1.0f / 6.0f), values[47]));

        bool customFresh = _lastTongueTick != 0 &&
            Environment.TickCount64 - _lastTongueTick <= TongueTimeoutMs;
        if (customFresh && _tongueEnabled)
        {
            // Only a new camera packet dirties the tongue slots; see the VD
            // bridge for why unchanged shapes are not rewritten every frame.
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
            Set((int)UnifiedExpressions.TongueOut, values[68]);
            Set((int)UnifiedExpressions.TongueCurlUp, values[64]);
        }
    }

    private void ApplyTongueOverride()
    {
        Set((int)UnifiedExpressions.TongueOut, _tongueValues[0]);
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

    // Same conversion as the Steam Link community modules: yaw and pitch of
    // the gaze point relative to the head's forward (-Z) axis, in radians.
    private static (float x, float y) GazePointToAngles(float[] point)
    {
        float x = MathF.Atan2(point[0], -point[2]);
        float y = MathF.Atan2(point[1], -point[2]);
        return (float.IsFinite(x) ? x : 0.0f, float.IsFinite(y) ? y : 0.0f);
    }
}
