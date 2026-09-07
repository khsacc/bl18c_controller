// MemoryPackEx.cs
//
// Mirrors (not submoduled) the two static helpers from
// seto77/Crystallography's ExtensionMethods.cs (MIT Licence, see
// LICENSE-Crystallography.txt), commit 99958eb4a5c5527134e8c303212acc349f5a6a6d,
// lines ~246-280. Copyright (c) 2002-2026 Yusuke SETO.
//
// This is the exact serialise-then-prefix-with-one-header-byte /
// strip-header-then-deserialise pair IPAnalyzer and PDIndexer use, so our
// payload bytes match theirs. BrotliCompressor/BrotliDecompressor are part
// of the MemoryPack NuGet package itself (MemoryPack.Compression), not
// Crystallography — no further mirroring needed for those.

using System.IO.Compression;
using MemoryPack.Compression;

namespace Crystallography;

public static class MemoryPackEx
{
    public static byte[] Serialize<T>(byte header, T val, CompressionLevel level = CompressionLevel.Optimal, int window = 22)
    {
        using var compressor = new BrotliCompressor(level, window);
        MemoryPackSerializer.Serialize(compressor, val);
        var data = compressor.ToArray();
        var buffer = new byte[data.Length + 1];
        buffer[0] = header;
        Buffer.BlockCopy(data, 0, buffer, 1, data.Length);
        return buffer;
    }

    public static T? Deserialize<T>(byte[] bytes)
    {
        try
        {
            using var decompressor = new BrotliDecompressor();
            return MemoryPackSerializer.Deserialize<T>(decompressor.Decompress(bytes));
        }
        catch
        {
            return default;
        }
    }
}
