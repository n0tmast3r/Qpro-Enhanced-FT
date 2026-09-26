using System.Buffers.Binary;
using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace QproFaceTracking.Hub;

// Builds "our own" independent-eye patch for the Quest Pro's on-device eye model.
//
// Independent eyes need two things, measured on firmware 51503870021300340 through Virtual
// Desktop: the model patch below AND debug.oculus.eye_tracking.social_filtering=0 (a
// filter in trackingservice that pulls both eyes onto a fixed depth). Either one alone
// leaves the vergence locked (stock model + property: -3 deg; patch alone: +3 deg); both
// together swung -9..+35 deg. The generated module sets that property at boot.
//
// The stock model (a PyTorch-Lite zip whose model/data.pkl embeds a HEXAGON graph as JSON
// plus a blob of constants) predicts each eye's gaze, then blends in the other eye through
// a gate: out = local + sigmoid(FC(x, W, b)) * (cross - local), reshaped into the public
// [1,4] gaze output. Independent eyes only need that gate switched off:
//   - Gate mode zeroes W and sets both biases to GateBias (sigmoid ~ 1e-13).
//   - Rewire mode points the public reshape at the local per-eye node (gate exactly 0).
// The gate is found by walking the graph from the public gaze output, never by fixed
// offsets, and every check fails closed. Nothing of Meta's is shipped: the hub reads the
// user's own model only to compute byte edits, and the generated Magisk module applies
// those edits to the headset's own copy on-device, verified by SHA-256.
internal static class EyeModelPatcher
{
    internal const string ModuleId = "qpro_eye_patch";
    internal const string ProductionModelPath = "/odm/etc/eyetracking/runtime/models/Seacliff_V1_5/fbnet/int8/bolt/bolt.ptl";
    internal const string ExperimentalModelPath = "/odm/etc/eyetracking/runtime/models/Seacliff_V1_5/fbnet/int8/experimental/bolt/bolt.ptl";
    internal const float GateBias = -30f;
    // Whether the generated module clears debug.oculus.eye_tracking.social_filtering at boot
    // (needed for independent eyes; the other *_filtering properties are not).
    internal const bool ClearSocialFilteringByDefault = true;

    // Stock models (SHA-256 -> firmware build) the patch has been measured on.
    internal static readonly IReadOnlyDictionary<string, string> TestedStockModels = new Dictionary<string, string>(StringComparer.Ordinal)
    {
        ["8868cbc3c19a002be2a2d32a7db6de72911dc24f3b95a527a5d747378d6628bb"] = "51503870021300340",
    };

    internal enum PatchMode { Gate, Rewire }

    internal sealed class PatchException(string message) : Exception(message);

    internal sealed record Plan(
        int DataStart,                  // model/data.pkl payload inside the archive (stored, not compressed)
        int DataLength,
        IReadOnlyList<int> CrcPositions, // every copy of data.pkl's CRC-32 in the archive
        int WeightOffset,
        int WeightLength,
        int BiasOffset,
        int BiasLength,
        int RewireOffset,               // same-length JSON edit: public reshape input -> local node
        byte[] RewireBytes,
        string Summary);

    internal static string Sha256(byte[] data) => Convert.ToHexString(SHA256.HashData(data)).ToLowerInvariant();

    internal static Plan Analyze(byte[] model)
    {
        var span = model.AsSpan();
        var eocd = span.LastIndexOf("PK\u0005\u0006"u8);
        if (eocd < 0 || eocd + 22 > span.Length) throw new PatchException("The eye model is not a zip archive.");
        int entries = U16(span, eocd + 10);
        var position = (int)U32(span, eocd + 16);
        int centralCrc = -1, localHeader = -1, size = -1, method = -1;
        uint storedCrc = 0;
        for (var index = 0; index < entries; index++)
        {
            Need(position + 46 <= span.Length && U32(span, position) == 0x02014B50, "The eye model's zip directory is damaged.");
            int nameLength = U16(span, position + 28), extraLength = U16(span, position + 30), commentLength = U16(span, position + 32);
            Need(position + 46 + nameLength <= span.Length, "The eye model's zip directory is damaged.");
            if (Encoding.UTF8.GetString(span.Slice(position + 46, nameLength)) == "model/data.pkl")
            {
                Need(centralCrc < 0, "The eye model's zip lists its graph twice.");
                method = U16(span, position + 10);
                storedCrc = U32(span, position + 16);
                Need(U32(span, position + 20) == U32(span, position + 24), "The eye model's graph is compressed; this layout is not supported.");
                size = (int)U32(span, position + 24);
                localHeader = (int)U32(span, position + 42);
                centralCrc = position + 16;
            }
            position += 46 + nameLength + extraLength + commentLength;
        }
        Need(centralCrc >= 0, "The eye model has no model/data.pkl.");
        Need(method == 0, "The eye model's graph is compressed; this layout is not supported.");
        Need(localHeader >= 0 && localHeader + 30 <= span.Length && U32(span, localHeader) == 0x04034B50, "The eye model's zip header is damaged.");
        int flags = U16(span, localHeader + 6);
        var dataStart = localHeader + 30 + U16(span, localHeader + 26) + U16(span, localHeader + 28);
        Need(dataStart + size <= span.Length, "The eye model's graph is truncated.");
        var payload = span.Slice(dataStart, size);
        Need(Crc32(payload) == storedCrc, "The eye model's checksum does not match its contents, so it has already been modified. Remove other eye modules, reboot, then try again.");
        var crcPositions = new List<int> { centralCrc };
        if (U32(span, localHeader + 14) == storedCrc) crcPositions.Add(localHeader + 14);
        if ((flags & 0x08) != 0)
        {
            var descriptor = dataStart + size;
            if (descriptor + 4 <= span.Length && U32(span, descriptor) == 0x08074B50) descriptor += 4;
            Need(descriptor + 4 <= span.Length && U32(span, descriptor) == storedCrc, "The eye model's zip descriptor is damaged.");
            crcPositions.Add(descriptor);
        }

        // The graph JSON is a pickled BINUNICODE string ('X' + uint32 length).
        var marker = "{\"version\": \"HEXAGON"u8;
        var jsonStart = payload.IndexOf(marker);
        Need(jsonStart >= 5 && payload[(jsonStart + 1)..].IndexOf(marker) < 0, "The eye model's graph was not found.");
        Need(payload[jsonStart - 5] == (byte)'X', "The eye model's graph is stored in an unexpected way.");
        var jsonLength = (int)U32(payload, jsonStart - 4);
        Need(jsonStart + jsonLength <= payload.Length, "The eye model's graph is truncated.");
        // The constants follow as the next long pickled string (BINUNICODE or BINBYTES).
        int blobStart = -1, blobLength = 0;
        for (var cursor = jsonStart + jsonLength; cursor < jsonStart + jsonLength + 64 && cursor + 5 <= payload.Length; cursor++)
        {
            if (payload[cursor] != (byte)'X' && payload[cursor] != (byte)'B') continue;
            var length = (int)U32(payload, cursor + 1);
            if (length > 1024 && cursor + 5 + length <= payload.Length) { blobStart = cursor + 5; blobLength = length; break; }
        }
        Need(blobStart > 0, "The eye model's constants were not found.");

        using var document = JsonDocument.Parse(model.AsMemory(dataStart + jsonStart, jsonLength));
        var root = document.RootElement;
        var nodes = new Dictionary<int, JsonElement>();
        foreach (var node in root.GetProperty("node").EnumerateArray()) nodes[node.GetProperty("id").GetInt32()] = node;
        var constants = new Dictionary<int, JsonElement>();
        foreach (var constant in root.GetProperty("const_node").EnumerateArray()) constants[constant.GetProperty("id").GetInt32()] = constant;

        // Walk back from the public [1,4] gaze reshape to the gate.
        var reshapes = nodes.Values.Where(node => Op(node) == "OP_Reshape" && OutputShape(node).SequenceEqual([1, 4])).ToList();
        Need(reshapes.Count == 1, "The eye model's gaze output has an unrecognized layout.");
        var reshape = reshapes[0];
        var reshapeId = reshape.GetProperty("id").GetInt32();
        var blendId = Inputs(reshape)[0];
        Need(nodes.TryGetValue(blendId, out var blend), "The eye model's gaze output has an unrecognized layout.");
        Need(Op(blend) == "OP_Add_f", "The eye model's gaze output does not come from the stock eye blend, so it already looks patched. Remove other eye modules, reboot, then try again.");
        var blendInputs = Inputs(blend);
        Need(blendInputs.Length == 2, "The eye model's eye blend has an unrecognized layout.");
        var mulIds = blendInputs.Where(id => nodes.TryGetValue(id, out var node) && Op(node) == "OP_Mul_f").ToList();
        Need(mulIds.Count == 1, "The eye model's eye blend has an unrecognized layout.");
        var localId = blendInputs.First(id => id != mulIds[0]);
        Need(nodes.TryGetValue(localId, out var local) && OutputShape(local).SequenceEqual(OutputShape(blend)), "The eye model's per-eye branch has an unrecognized layout.");
        var sigmoidIds = Inputs(nodes[mulIds[0]]).Where(id => nodes.TryGetValue(id, out var node) && Op(node).Contains("Sigmoid", StringComparison.Ordinal)).ToList();
        Need(sigmoidIds.Count == 1, "The eye model's blend gate has an unrecognized layout.");
        var fc = nodes[Inputs(nodes[sigmoidIds[0]])[0]];
        for (var hop = 0; hop < 3 && Op(fc) == "OP_Reshape"; hop++) fc = nodes[Inputs(fc)[0]];
        Need(Op(fc) == "OP_FC_f" && Inputs(fc).Length == 3, "The eye model's blend gate has an unrecognized layout.");
        var weightId = Inputs(fc)[1];
        var biasId = Inputs(fc)[2];
        if (!constants.TryGetValue(weightId, out var weight) || !constants.TryGetValue(biasId, out var bias))
            throw new PatchException("The eye model's gate constants were not found.");
        var weightShape = Shape(weight);
        var (weightLocation, weightLength) = Location(weight);
        var (biasLocation, biasLength) = Location(bias);
        Need(weightShape.Length == 2 && weightShape[0] == 2 && weightLength == 4 * weightShape[0] * weightShape[1], "The eye model's gate weights have an unexpected shape.");
        Need(Shape(bias).SequenceEqual([2]) && biasLength == 8, "The eye model's gate bias has an unexpected shape.");
        Need(weightLocation >= 0 && weightLocation + weightLength <= blobLength && biasLocation >= 0 && biasLocation + biasLength <= blobLength, "The eye model's gate constants are out of range.");
        var weightOffset = dataStart + blobStart + weightLocation;
        var biasOffset = dataStart + blobStart + biasLocation;
        Need(span.Slice(weightOffset, weightLength).ContainsAnyExcept((byte)0), "The eye model's gate is already switched off, so it already looks patched. Remove other eye modules, reboot, then try again.");

        // Rewire: the reshape node's input text, replaced by the same number of bytes.
        var json = payload.Slice(jsonStart, jsonLength);
        // "id" alone is not unique in the text (nested entries reuse it), so anchor on id + name.
        var reshapeName = reshape.TryGetProperty("name", out var nameElement) ? nameElement.GetString() ?? "" : "";
        Need(reshapeName.Length > 0, "The eye model's gaze output has an unrecognized layout.");
        var idText = Encoding.UTF8.GetBytes($"\"id\": {reshapeId}, \"name\": {JsonSerializer.Serialize(reshapeName)},");
        var nodeAt = json.IndexOf(idText);
        Need(nodeAt >= 0 && json[(nodeAt + 1)..].IndexOf(idText) < 0, "The eye model's gaze output text was not found.");
        var nextNode = json[(nodeAt + idText.Length)..].IndexOf("\"id\":"u8);
        var nodeText = json.Slice(nodeAt, nextNode < 0 ? json.Length - nodeAt : idText.Length + nextNode);
        var oldInput = Encoding.ASCII.GetBytes($"\"input\": [[{blendId}, 0]");
        var inputAt = nodeText.IndexOf(oldInput);
        Need(inputAt >= 0 && nodeText[(inputAt + 1)..].IndexOf(oldInput) < 0, "The eye model's gaze output text was not found.");
        var rewire = SameLengthInput(localId, oldInput.Length);
        var rewireOffset = dataStart + jsonStart + nodeAt + inputAt;
        var rewired = json.ToArray();
        rewire.CopyTo(rewired, nodeAt + inputAt);
        using (var check = JsonDocument.Parse(rewired))
        {
            var patchedReshape = check.RootElement.GetProperty("node").EnumerateArray().First(node => node.GetProperty("id").GetInt32() == reshapeId);
            Need(Inputs(patchedReshape)[0] == localId, "The rewire patch did not verify.");
        }

        return new Plan(
            dataStart, size, crcPositions,
            weightOffset, weightLength, biasOffset, biasLength,
            rewireOffset, rewire,
            $"gaze reshape {reshapeId} <- blend {blendId} (local {localId}); gate FC {fc.GetProperty("id").GetInt32()} weight [{weightShape[0]},{weightShape[1]}] @{weightOffset}, bias @{biasOffset}");
    }

    internal static byte[] Apply(byte[] model, Plan plan, PatchMode mode, float gateBias = GateBias, bool fixCrc = true)
    {
        var patched = (byte[])model.Clone();
        if (mode == PatchMode.Gate)
        {
            Array.Clear(patched, plan.WeightOffset, plan.WeightLength);
            for (var index = 0; index < plan.BiasLength / 4; index++)
                BinaryPrimitives.WriteSingleLittleEndian(patched.AsSpan(plan.BiasOffset + 4 * index), gateBias);
        }
        else
        {
            plan.RewireBytes.CopyTo(patched, plan.RewireOffset);
        }
        if (fixCrc)
        {
            var crc = Crc32(patched.AsSpan(plan.DataStart, plan.DataLength));
            foreach (var position in plan.CrcPositions) BinaryPrimitives.WriteUInt32LittleEndian(patched.AsSpan(position), crc);
        }
        return patched;
    }

    // A Magisk module that applies the edits to the headset's own stock model on-device.
    // It contains shell scripts and a few bytes of patch data, never model content.
    internal static byte[] BuildModule(byte[] stock, byte[] patched, PatchMode mode, string firmware, bool clearSocialFiltering)
    {
        Need(stock.Length == patched.Length, "The patch changed the model size.");
        var stockSha = Sha256(stock);
        var patchedSha = Sha256(patched);
        var modeName = mode == PatchMode.Gate ? "gate" : "rewire";
        var safeFirmware = new string(firmware.Where(char.IsAsciiLetterOrDigit).Take(40).ToArray());

        var patch = new StringBuilder();
        patch.Append("#!/system/bin/sh\n");
        patch.Append($"# Generated by QproFaceTracking: byte edits ({modeName} patch) for this headset's own eye model.\n");
        patch.Append("F=\"$1\"\n[ -f \"$F\" ] || exit 1\n");
        foreach (var (offset, bytes) in DiffRuns(stock, patched))
        {
            // Long zero spans come from /dev/zero; everything else is written as octal escapes.
            var index = 0;
            while (index < bytes.Length)
            {
                var zeros = 0;
                while (index + zeros < bytes.Length && bytes[index + zeros] == 0) zeros++;
                if (zeros >= 16)
                {
                    patch.Append($"dd if=/dev/zero of=\"$F\" bs=1 seek={offset + index} count={zeros} conv=notrunc 2>/dev/null || exit 1\n");
                    index += zeros;
                    continue;
                }
                var end = index;
                while (end < bytes.Length)
                {
                    var run = 0;
                    while (end + run < bytes.Length && bytes[end + run] == 0) run++;
                    if (run >= 16) break;
                    end += Math.Max(run, 1);
                }
                patch.Append($"printf '{Octal(bytes.AsSpan(index, end - index))}' | dd of=\"$F\" bs=1 seek={offset + index} conv=notrunc 2>/dev/null || exit 1\n");
                index = end;
            }
        }
        patch.Append("exit 0\n");

        var files = new Dictionary<string, string>(StringComparer.Ordinal)
        {
            ["module.prop"] =
                $"id={ModuleId}\n" +
                "name=Qpro Independent Eye Patch\n" +
                $"version=v1-{modeName}\n" +
                "versionCode=1\n" +
                "author=QproFaceTracking (built on this headset)\n" +
                $"description=Independent per-eye gaze: patches this headset's own eye model on-device ({modeName} patch, firmware {safeFirmware}). Contains no Meta files. Remove to revert.\n",
            ["qpro-eye.conf"] =
                $"# Generated by QproFaceTracking for firmware {safeFirmware}.\n" +
                $"MODE={modeName}\n" +
                $"STOCK_SHA={stockSha}\n" +
                $"PATCHED_SHA={patchedSha}\n" +
                $"TARGET={ProductionModelPath}\n" +
                $"CANDIDATES=\"{ProductionModelPath} {ExperimentalModelPath}\"\n" +
                $"CLEAR_SOCIAL_FILTERING={(clearSocialFiltering ? 1 : 0)}\n",
            ["patch.sh"] = patch.ToString(),
            ["customize.sh"] =
                "# Generated by QproFaceTracking. Copies THIS headset's own stock eye model, applies\n" +
                "# patch.sh (offsets computed from that same model) and verifies the result by SHA-256.\n" +
                "SKIPUNZIP=0\n" +
                ". \"$MODPATH/qpro-eye.conf\"\n" +
                "ui_print \"- Qpro Independent Eye Patch ($MODE)\"\n" +
                "SRC=\"\"\n" +
                "for f in $CANDIDATES; do\n" +
                "  if [ -f \"$f\" ] && [ \"$(sha256sum \"$f\" | cut -d' ' -f1)\" = \"$STOCK_SHA\" ]; then SRC=\"$f\"; break; fi\n" +
                "done\n" +
                "[ -n \"$SRC\" ] || abort \"! This headset's stock eye model was not found (firmware changed?). Rebuild the patch in QproFaceTracking.\"\n" +
                "OUT=\"$MODPATH/bolt.ptl\"\n" +
                "cp -f \"$SRC\" \"$OUT\" || abort \"! Could not copy the eye model.\"\n" +
                "sh \"$MODPATH/patch.sh\" \"$OUT\" || { rm -f \"$OUT\"; abort \"! Patching failed; nothing was changed.\"; }\n" +
                "if [ \"$(sha256sum \"$OUT\" | cut -d' ' -f1)\" != \"$PATCHED_SHA\" ]; then rm -f \"$OUT\"; abort \"! The patched model did not verify; nothing was changed.\"; fi\n" +
                "set_perm \"$OUT\" 0 0 0644 u:object_r:vendor_configs_file:s0\n" +
                "ui_print \"- Patched model verified. Reboot the headset to activate it, then redo eye calibration.\"\n",
            ["post-fs-data.sh"] =
                "MODDIR=${0%/*}\n" +
                ". \"$MODDIR/qpro-eye.conf\"\n" +
                "STATUS=\"$MODDIR/qpro_status\"\n" +
                "OUT=\"$MODDIR/bolt.ptl\"\n" +
                "# trackingservice builds its filter chain when it starts; set the property before that.\n" +
                "[ \"$CLEAR_SOCIAL_FILTERING\" = 1 ] && resetprop debug.oculus.eye_tracking.social_filtering 0\n" +
                "[ -f \"$OUT\" ] || { echo \"inactive: patched model missing, reinstall\" > \"$STATUS\"; exit 0; }\n" +
                "[ \"$(sha256sum \"$OUT\" | cut -d' ' -f1)\" = \"$PATCHED_SHA\" ] || { echo \"inactive: patched model damaged, reinstall\" > \"$STATUS\"; exit 0; }\n" +
                "CUR=$(sha256sum \"$TARGET\" | cut -d' ' -f1)\n" +
                "if [ \"$CUR\" = \"$PATCHED_SHA\" ]; then echo mounted > \"$STATUS\"; exit 0; fi\n" +
                "[ \"$CUR\" = \"$STOCK_SHA\" ] || { echo \"inactive: the eye model changed (firmware update or another module), rebuild the patch\" > \"$STATUS\"; exit 0; }\n" +
                "chcon u:object_r:vendor_configs_file:s0 \"$OUT\"\n" +
                "mount -o bind \"$OUT\" \"$TARGET\" || { echo \"inactive: bind mount failed\" > \"$STATUS\"; exit 0; }\n" +
                "if [ \"$(sha256sum \"$TARGET\" | cut -d' ' -f1)\" != \"$PATCHED_SHA\" ]; then umount \"$TARGET\"; echo \"inactive: mount did not verify\" > \"$STATUS\"; exit 0; fi\n" +
                "echo mounted > \"$STATUS\"\n" +
                "# trackingservice reads the model when it starts. If it is already running (root applied\n" +
                "# after boot), service.sh restarts it once so it loads the patched model.\n" +
                "pidof trackingservice >/dev/null && touch \"$MODDIR/.restart\"\n" +
                "exit 0\n",
            ["service.sh"] =
                "MODDIR=${0%/*}\n" +
                ". \"$MODDIR/qpro-eye.conf\"\n" +
                "STATUS=\"$MODDIR/qpro_status\"\n" +
                "OUT=\"$MODDIR/bolt.ptl\"\n" +
                "until [ \"$(getprop sys.boot_completed)\" = 1 ]; do sleep 1; done\n" +
                "RESTART=0\n" +
                "[ -f \"$MODDIR/.restart\" ] && RESTART=1\n" +
                "rm -f \"$MODDIR/.restart\"\n" +
                "[ -f \"$OUT\" ] && [ \"$(sha256sum \"$OUT\" | cut -d' ' -f1)\" = \"$PATCHED_SHA\" ] || exit 0\n" +
                "# A later mount (e.g. the Magisk OverlayFS module overlaying /odm/etc) can hide the\n" +
                "# early bind mount. Check what the path serves now and mount again on top if needed.\n" +
                "CUR=$(sha256sum \"$TARGET\" | cut -d' ' -f1)\n" +
                "if [ \"$CUR\" != \"$PATCHED_SHA\" ]; then\n" +
                "  [ \"$CUR\" = \"$STOCK_SHA\" ] || { echo \"inactive: the eye model changed (firmware update or another module), rebuild the patch\" > \"$STATUS\"; exit 0; }\n" +
                "  chcon u:object_r:vendor_configs_file:s0 \"$OUT\"\n" +
                "  mount -o bind \"$OUT\" \"$TARGET\" || { echo \"inactive: bind mount failed\" > \"$STATUS\"; exit 0; }\n" +
                "  if [ \"$(sha256sum \"$TARGET\" | cut -d' ' -f1)\" != \"$PATCHED_SHA\" ]; then umount \"$TARGET\"; echo \"inactive: mount did not verify\" > \"$STATUS\"; exit 0; fi\n" +
                "  echo mounted > \"$STATUS\"\n" +
                "  RESTART=1\n" +
                "fi\n" +
                "[ \"$RESTART\" = 1 ] || exit 0\n" +
                "[ \"$CLEAR_SOCIAL_FILTERING\" = 1 ] && resetprop debug.oculus.eye_tracking.social_filtering 0\n" +
                "OLD=$(pidof trackingservice)\n" +
                "[ -n \"$OLD\" ] || exit 0\n" +
                "# init's restart (runs onrestart hooks); trackingservice only.\n" +
                "setprop ctl.restart trackingservice\n" +
                "NEW=$OLD; i=0\n" +
                "while [ $i -lt 20 ]; do sleep 1; NEW=$(pidof trackingservice); [ -n \"$NEW\" ] && [ \"$NEW\" != \"$OLD\" ] && break; i=$((i+1)); done\n" +
                "echo \"trackingservice $OLD -> ${NEW:-not running}\" > \"$MODDIR/qpro_restart.log\"\n",
            ["uninstall.sh"] =
                "MODDIR=${0%/*}\n" +
                ". \"$MODDIR/qpro-eye.conf\" 2>/dev/null\n" +
                "if [ -n \"$TARGET\" ] && [ \"$(sha256sum \"$TARGET\" | cut -d' ' -f1)\" = \"$PATCHED_SHA\" ]; then umount \"$TARGET\"; fi\n",
        };

        using var buffer = new MemoryStream();
        using (var archive = new ZipArchive(buffer, ZipArchiveMode.Create, leaveOpen: true))
        {
            foreach (var (name, text) in files)
            {
                var entry = archive.CreateEntry(name, CompressionLevel.Optimal);
                using var stream = entry.Open();
                var bytes = Encoding.ASCII.GetBytes(text.Replace("\r\n", "\n"));
                stream.Write(bytes);
            }
        }
        return buffer.ToArray();
    }

    // Contiguous changed ranges; nearby ranges are merged (writing unchanged bytes back as-is).
    private static List<(int Offset, byte[] Bytes)> DiffRuns(byte[] before, byte[] after)
    {
        var runs = new List<(int Offset, byte[] Bytes)>();
        int start = -1, last = -1;
        for (var index = 0; index < before.Length; index++)
        {
            if (before[index] == after[index]) continue;
            if (start >= 0 && index - last <= 32) { last = index; continue; }
            if (start >= 0) runs.Add((start, after[start..(last + 1)]));
            start = last = index;
        }
        if (start >= 0) runs.Add((start, after[start..(last + 1)]));
        return runs;
    }

    private static string Octal(ReadOnlySpan<byte> bytes)
    {
        var text = new StringBuilder(bytes.Length * 4);
        foreach (var value in bytes) text.Append('\\').Append(Convert.ToString(value, 8).PadLeft(3, '0'));
        return text.ToString();
    }

    // `"input": [[<local>, 0]` padded (JSON whitespace) to exactly `length` bytes.
    private static byte[] SameLengthInput(int localId, int length)
    {
        foreach (var core in new[] { $"[[{localId}, 0]", $"[[{localId},0]" })
            foreach (var key in new[] { "\"input\": ", "\"input\":" })
                if (key.Length + core.Length <= length)
                    return Encoding.ASCII.GetBytes(key + new string(' ', length - key.Length - core.Length) + core);
        throw new PatchException("The rewire patch does not fit this model.");
    }

    private static string Op(JsonElement node) => node.TryGetProperty("op", out var op) ? op.GetString() ?? "" : "";
    private static int[] Inputs(JsonElement node) => node.GetProperty("input").EnumerateArray().Select(pair => pair[0].GetInt32()).ToArray();
    private static int[] OutputShape(JsonElement node) =>
        node.TryGetProperty("output", out var output) && output.GetArrayLength() > 0 ? Shape(output[0]) : [];
    private static int[] Shape(JsonElement element) =>
        element.TryGetProperty("shape", out var shape) ? shape.EnumerateArray().Select(value => value.GetInt32()).ToArray() : [];
    private static (int Offset, int Length) Location(JsonElement constant)
    {
        var location = constant.GetProperty("location");
        return (location[0].GetInt32(), location[1].GetInt32());
    }

    private static void Need(bool condition, string reason)
    {
        if (!condition) throw new PatchException(reason);
    }

    private static int U16(ReadOnlySpan<byte> data, int offset) => BinaryPrimitives.ReadUInt16LittleEndian(data[offset..]);
    private static uint U32(ReadOnlySpan<byte> data, int offset) => BinaryPrimitives.ReadUInt32LittleEndian(data[offset..]);

    private static readonly uint[] CrcTable = BuildCrcTable();
    private static uint[] BuildCrcTable()
    {
        var table = new uint[256];
        for (uint value = 0; value < 256; value++)
        {
            var crc = value;
            for (var bit = 0; bit < 8; bit++) crc = (crc & 1) != 0 ? 0xEDB88320u ^ (crc >> 1) : crc >> 1;
            table[value] = crc;
        }
        return table;
    }

    internal static uint Crc32(ReadOnlySpan<byte> data)
    {
        var crc = 0xFFFFFFFFu;
        foreach (var value in data) crc = CrcTable[(crc ^ value) & 0xFF] ^ (crc >> 8);
        return crc ^ 0xFFFFFFFFu;
    }
}
