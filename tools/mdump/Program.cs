using System;
using System.Collections.Generic;
using System.Collections.Immutable;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Reflection.Emit;
using System.Reflection.Metadata;
using System.Reflection.Metadata.Ecma335;
using System.Reflection.PortableExecutable;
using System.Text;
using System.Text.RegularExpressions;

// Offline inspector for CS2's managed assemblies.
//   mdump members <dllGlob> <typeRegex> [memberRegex]
//   mdump strings <dllGlob> <regex>
//   mdump il      <dllGlob> <typeRegex> <methodRegex>
//   mdump find    <dllGlob> <regex>            -- methods whose IL mentions a matching member/string
//   mdump types   <dllGlob> <typeRegex>

static class Program
{
    static string Managed = Environment.GetEnvironmentVariable("CSII_MANAGEDPATH")
        ?? @"G:\SteamLibrary\steamapps\common\Cities Skylines II\Cities2_Data\Managed";

    static int Main(string[] argv)
    {
        if (argv.Length < 2) { Console.Error.WriteLine("usage: mdump <members|strings|il|find|types> <dllGlob> [args]"); return 2; }
        string mode = argv[0], glob = argv[1];
        var files = Directory.GetFiles(Managed, glob.EndsWith(".dll") ? glob : glob + ".dll");
        if (files.Length == 0) { Console.Error.WriteLine($"no assembly matched {glob} in {Managed}"); return 2; }

        foreach (var file in files)
        {
            using var fs = File.OpenRead(file);
            using var pe = new PEReader(fs);
            if (!pe.HasMetadata) continue;
            var md = pe.GetMetadataReader();
            string asm = Path.GetFileName(file);
            switch (mode)
            {
                case "members": Members(md, asm, argv[2], argv.Length > 3 ? argv[3] : null); break;
                case "types":   Types(md, asm, argv[2]); break;
                case "strings": Strings(md, asm, argv[2]); break;
                case "il":      Il(pe, md, asm, argv[2], argv[3]); break;
                case "find":    Find(pe, md, asm, argv[2]); break;
                default: Console.Error.WriteLine("bad mode"); return 2;
            }
        }
        return 0;
    }

    static string FullName(MetadataReader md, TypeDefinition td)
    {
        string n = md.GetString(td.Name);
        string ns = md.GetString(td.Namespace);
        if (td.IsNested)
        {
            var parent = md.GetTypeDefinition(td.GetDeclaringType());
            return FullName(md, parent) + "+" + n;
        }
        return string.IsNullOrEmpty(ns) ? n : ns + "." + n;
    }

    static void Types(MetadataReader md, string asm, string typeRegex)
    {
        var rx = new Regex(typeRegex, RegexOptions.IgnoreCase);
        foreach (var h in md.TypeDefinitions)
        {
            var td = md.GetTypeDefinition(h);
            string full = FullName(md, td);
            if (rx.IsMatch(full)) Console.WriteLine($"{asm}: {full}");
        }
    }

    static void Members(MetadataReader md, string asm, string typeRegex, string memberRegex)
    {
        var rx = new Regex(typeRegex, RegexOptions.IgnoreCase);
        var mrx = memberRegex == null ? null : new Regex(memberRegex, RegexOptions.IgnoreCase);
        var prov = new Sig(md);
        foreach (var h in md.TypeDefinitions)
        {
            var td = md.GetTypeDefinition(h);
            string full = FullName(md, td);
            if (!rx.IsMatch(full)) continue;

            string base_ = "";
            if (!td.BaseType.IsNil) base_ = " : " + TypeRefName(md, td.BaseType);
            var ifaces = td.GetInterfaceImplementations()
                .Select(i => TypeRefName(md, md.GetInterfaceImplementation(i).Interface)).ToList();
            if (ifaces.Count > 0) base_ += (base_ == "" ? " : " : ", ") + string.Join(", ", ifaces);
            Console.WriteLine($"\n=== {asm}: {full}{base_} [{td.Attributes}]");

            foreach (var fh in td.GetFields())
            {
                var fd = md.GetFieldDefinition(fh);
                string name = md.GetString(fd.Name);
                if (mrx != null && !mrx.IsMatch(name)) continue;
                Console.WriteLine($"  F {fd.Attributes,-30} {fd.DecodeSignature(prov, null)} {name}{ConstSuffix(md, fd)}");
            }
            foreach (var ph in td.GetProperties())
            {
                var pd = md.GetPropertyDefinition(ph);
                string name = md.GetString(pd.Name);
                if (mrx != null && !mrx.IsMatch(name)) continue;
                var s = pd.DecodeSignature(prov, null);
                var acc = pd.GetAccessors();
                string kind = (acc.Getter.IsNil ? "" : "get;") + (acc.Setter.IsNil ? "" : "set;");
                Console.WriteLine($"  P {s.ReturnType} {name} {{{kind}}}");
            }
            foreach (var mh in td.GetMethods())
            {
                var mdf = md.GetMethodDefinition(mh);
                string name = md.GetString(mdf.Name);
                if (mrx != null && !mrx.IsMatch(name)) continue;
                var s = mdf.DecodeSignature(prov, null);
                Console.WriteLine($"  M {(mdf.Attributes.HasFlag(MethodAttributes.Static) ? "static " : "")}{s.ReturnType} {name}({string.Join(", ", s.ParameterTypes)})");
            }
        }
    }

    static string TypeRefName(MetadataReader md, EntityHandle h)
    {
        switch (h.Kind)
        {
            case HandleKind.TypeDefinition: return FullName(md, md.GetTypeDefinition((TypeDefinitionHandle)h));
            case HandleKind.TypeReference:
                var tr = md.GetTypeReference((TypeReferenceHandle)h);
                string ns = md.GetString(tr.Namespace);
                return (string.IsNullOrEmpty(ns) ? "" : ns + ".") + md.GetString(tr.Name);
            case HandleKind.TypeSpecification:
                try { return md.GetTypeSpecification((TypeSpecificationHandle)h).DecodeSignature(new Sig(md), null); }
                catch { return "<spec>"; }
            default: return "<?>";
        }
    }

    static void Strings(MetadataReader md, string asm, string regex)
    {
        var rx = new Regex(regex, RegexOptions.IgnoreCase);
        var h = default(UserStringHandle);
        var seen = new HashSet<string>();
        while (true)
        {
            h = md.GetNextHandle(h);
            if (h.IsNil) break;
            string s = md.GetUserString(h);
            if (s.Length == 0 || !rx.IsMatch(s)) continue;
            if (seen.Add(s)) Console.WriteLine($"{asm}: \"{s}\"");
        }
    }

    // ---- literal field values --------------------------------------------

    /// <summary>
    /// Decodes a literal field's constant into " = <value>", with a hex form for
    /// integers so that flag-enum bit values are readable at a glance. Enum members
    /// compile to bare <c>ldc.i4</c> in IL, so a bit test is only recognisable once
    /// its numeric value is known; without this the <c>find</c> mode cannot see them.
    /// Returns "" for a field that has no constant.
    /// </summary>
    static string ConstSuffix(MetadataReader md, FieldDefinition fd)
    {
        var ch = fd.GetDefaultValue();
        if (ch.IsNil) return "";
        var c = md.GetConstant(ch);
        var br = md.GetBlobReader(c.Value);
        switch (c.TypeCode)
        {
            case ConstantTypeCode.SByte: return Num(br.ReadSByte());
            case ConstantTypeCode.Byte: return Num(br.ReadByte());
            case ConstantTypeCode.Int16: return Num(br.ReadInt16());
            case ConstantTypeCode.UInt16: return Num(br.ReadUInt16());
            case ConstantTypeCode.Int32: return Num(br.ReadInt32());
            case ConstantTypeCode.UInt32: return Num(br.ReadUInt32());
            case ConstantTypeCode.Int64: return Num(br.ReadInt64());
            case ConstantTypeCode.UInt64: return Num((long)br.ReadUInt64());
            case ConstantTypeCode.Boolean: return " = " + br.ReadBoolean();
            case ConstantTypeCode.Single: return " = " + br.ReadSingle();
            case ConstantTypeCode.Double: return " = " + br.ReadDouble();
            case ConstantTypeCode.String: return " = \"" + br.ReadUTF16(br.Length) + "\"";
            default: return "";
        }

        static string Num(long v) => v >= 0 ? $" = {v} (0x{v:X})" : $" = {v}";
    }

    // ---- crude IL walker -------------------------------------------------

    static readonly Dictionary<int, int> OperandSize = BuildOperandSizes();

    static Dictionary<int, int> BuildOperandSizes()
    {
        var d = new Dictionary<int, int>();
        foreach (var f in typeof(OpCodes).GetFields(BindingFlags.Public | BindingFlags.Static))
        {
            var op = (OpCode)f.GetValue(null);
            int key = op.Size == 1 ? (op.Value & 0xFF) : (0xFE00 | (op.Value & 0xFF));
            d[key] = op.OperandType switch
            {
                OperandType.InlineNone => 0,
                OperandType.ShortInlineBrTarget or OperandType.ShortInlineI or OperandType.ShortInlineVar => 1,
                OperandType.InlineVar => 2,
                OperandType.InlineBrTarget or OperandType.InlineField or OperandType.InlineI
                    or OperandType.InlineMethod or OperandType.InlineSig or OperandType.InlineString
                    or OperandType.InlineTok or OperandType.InlineType or OperandType.ShortInlineR => 4,
                OperandType.InlineI8 or OperandType.InlineR => 8,
                OperandType.InlineSwitch => -1,
                _ => 0,
            };
        }
        return d;
    }

    static string MemberName(MetadataReader md, EntityHandle h)
    {
        try
        {
            switch (h.Kind)
            {
                case HandleKind.MethodDefinition:
                {
                    var m = md.GetMethodDefinition((MethodDefinitionHandle)h);
                    return FullName(md, md.GetTypeDefinition(m.GetDeclaringType())) + "::" + md.GetString(m.Name);
                }
                case HandleKind.FieldDefinition:
                {
                    var f = md.GetFieldDefinition((FieldDefinitionHandle)h);
                    return FullName(md, md.GetTypeDefinition(f.GetDeclaringType())) + "::" + md.GetString(f.Name);
                }
                case HandleKind.MemberReference:
                {
                    var mr = md.GetMemberReference((MemberReferenceHandle)h);
                    return TypeRefName(md, mr.Parent) + "::" + md.GetString(mr.Name);
                }
                case HandleKind.MethodSpecification:
                {
                    var ms = md.GetMethodSpecification((MethodSpecificationHandle)h);
                    return MemberName(md, ms.Method) + "<>";
                }
                case HandleKind.TypeDefinition:
                case HandleKind.TypeReference:
                case HandleKind.TypeSpecification:
                    return TypeRefName(md, h);
                default: return h.Kind.ToString();
            }
        }
        catch { return "<err>"; }
    }

    struct Op { public int Offset; public string Name; public string Arg; }

    static List<Op> Body(PEReader pe, MetadataReader md, MethodDefinition mdf)
    {
        var ops = new List<Op>();
        if (mdf.RelativeVirtualAddress == 0) return ops;
        var body = pe.GetMethodBody(mdf.RelativeVirtualAddress);
        var il = body.GetILBytes();
        int i = 0;
        while (i < il.Length)
        {
            int start = i;
            int code = il[i++];
            if (code == 0xFE) { code = 0xFE00 | il[i++]; }
            if (!OperandSize.TryGetValue(code, out int size)) break; // desync guard
            string name = OpName(code);
            string arg = null;
            if (size == -1)
            {
                int n = BitConverter.ToInt32(il, i); i += 4 + 4 * n;
            }
            else if (size == 4 && (name.StartsWith("call") || name is "newobj" or "ldftn" or "ldvirtftn"
                     || name.StartsWith("ldfld") || name.StartsWith("stfld") || name.StartsWith("ldsfld")
                     || name.StartsWith("stsfld") || name is "ldtoken" or "castclass" or "isinst" or "newarr"
                     || name is "box" or "unbox.any" or "initobj" or "ldobj" or "stobj" or "constrained."))
            {
                int tok = BitConverter.ToInt32(il, i); i += 4;
                arg = MemberName(md, MetadataTokens.EntityHandle(tok));
            }
            else if (size == 4 && name == "ldstr")
            {
                int tok = BitConverter.ToInt32(il, i); i += 4;
                try { arg = "\"" + md.GetUserString(MetadataTokens.UserStringHandle(tok & 0xFFFFFF)) + "\""; }
                catch { arg = "<str>"; }
            }
            // Branch targets. Without these a `brfalse` is unreadable: there is no way to tell
            // a jump that skips one step from one that skips the whole loop body, which is
            // exactly the difference between "adjust this piece" and "do not emit this piece".
            else if (name.StartsWith("b") && (size == 1 || size == 4)
                     && (name.StartsWith("br") || name.StartsWith("beq") || name.StartsWith("bge")
                         || name.StartsWith("bgt") || name.StartsWith("ble") || name.StartsWith("blt")
                         || name.StartsWith("bne")))
            {
                int delta = size == 1 ? (sbyte)il[i] : BitConverter.ToInt32(il, i);
                i += size;
                arg = $"-> IL_{i + delta:X4}";
            }
            else if (name == "leave" || name == "leave.s")
            {
                int delta = size == 1 ? (sbyte)il[i] : BitConverter.ToInt32(il, i);
                i += size;
                arg = $"-> IL_{i + delta:X4}";
            }
            // Numeric literals. A flag test compiles to `ldc.i4 <bits>; and`, so without the
            // operand a bit test is indistinguishable from any other `and` — which is what
            // hid the Hidden*/MeshLayer masks. Hex is printed alongside for mask reading.
            else if (name == "ldc.i4" && size == 4)
            {
                int v = BitConverter.ToInt32(il, i); i += 4;
                arg = v >= 0 ? $"{v} (0x{v:X})" : v.ToString();
            }
            else if (name == "ldc.i4.s" && size == 1)
            {
                int v = (sbyte)il[i]; i += 1;
                arg = v >= 0 ? $"{v} (0x{v:X})" : v.ToString();
            }
            else if (name == "ldc.r4" && size == 4)
            {
                arg = BitConverter.ToSingle(il, i).ToString("R"); i += 4;
            }
            else if (name == "ldc.r8" && size == 8)
            {
                arg = BitConverter.ToDouble(il, i).ToString("R"); i += 8;
            }
            else if (name == "ldc.i8" && size == 8)
            {
                arg = BitConverter.ToInt64(il, i).ToString(); i += 8;
            }
            else i += size;
            if (arg == null && name.StartsWith("ldc.i4.") && name.Length > 7)
            {
                // ldc.i4.0 .. ldc.i4.8 and ldc.i4.M1 carry the value in the opcode itself.
                string t = name.Substring(7);
                if (t == "M1") arg = "-1";
                else if (t.Length == 1 && t[0] >= '0' && t[0] <= '8') arg = t;
            }
            ops.Add(new Op { Offset = start, Name = name, Arg = arg });
        }
        return ops;
    }

    static Dictionary<int, string> _names;
    static string OpName(int code)
    {
        if (_names == null)
        {
            _names = new Dictionary<int, string>();
            foreach (var f in typeof(OpCodes).GetFields(BindingFlags.Public | BindingFlags.Static))
            {
                var op = (OpCode)f.GetValue(null);
                int key = op.Size == 1 ? (op.Value & 0xFF) : (0xFE00 | (op.Value & 0xFF));
                _names[key] = op.Name;
            }
        }
        return _names.TryGetValue(code, out var n) ? n : $"op_{code:X}";
    }

    static void Il(PEReader pe, MetadataReader md, string asm, string typeRegex, string methodRegex)
    {
        var rx = new Regex(typeRegex, RegexOptions.IgnoreCase);
        var mrx = new Regex(methodRegex, RegexOptions.IgnoreCase);
        var prov = new Sig(md);
        foreach (var h in md.TypeDefinitions)
        {
            var td = md.GetTypeDefinition(h);
            string full = FullName(md, td);
            if (!rx.IsMatch(full)) continue;
            foreach (var mh in td.GetMethods())
            {
                var mdf = md.GetMethodDefinition(mh);
                string name = md.GetString(mdf.Name);
                if (!mrx.IsMatch(name)) continue;
                var s = mdf.DecodeSignature(prov, null);
                Console.WriteLine($"\n=== {asm}: {full}::{name}({string.Join(", ", s.ParameterTypes)}) -> {s.ReturnType}");
                bool all = Environment.GetEnvironmentVariable("MDUMP_ALL") == "1";
                foreach (var op in Body(pe, md, mdf))
                    if (op.Arg != null) Console.WriteLine($"  IL_{op.Offset:X4} {op.Name,-12} {op.Arg}");
                    else if (all || op.Name is "ret" or "throw" or "ldnull" or "ldc.i4.0" or "ldc.i4.1")
                        Console.WriteLine($"  IL_{op.Offset:X4} {op.Name}");
            }
        }
    }

    static void Find(PEReader pe, MetadataReader md, string asm, string regex)
    {
        var rx = new Regex(regex, RegexOptions.IgnoreCase);
        foreach (var h in md.TypeDefinitions)
        {
            var td = md.GetTypeDefinition(h);
            string full = FullName(md, td);
            foreach (var mh in td.GetMethods())
            {
                var mdf = md.GetMethodDefinition(mh);
                List<Op> ops;
                try { ops = Body(pe, md, mdf); } catch { continue; }
                var hits = ops.Where(o => o.Arg != null && rx.IsMatch(o.Arg)).ToList();
                if (hits.Count == 0) continue;
                Console.WriteLine($"\n--- {asm}: {full}::{md.GetString(mdf.Name)}");
                foreach (var o in hits.Take(24)) Console.WriteLine($"      {o.Name,-12} {o.Arg}");
            }
        }
    }

    // ---- signature decoding ---------------------------------------------

    class Sig : ISignatureTypeProvider<string, object>
    {
        readonly MetadataReader _md;
        public Sig(MetadataReader md) { _md = md; }
        public string GetArrayType(string e, ArrayShape s) => e + "[" + new string(',', s.Rank - 1) + "]";
        public string GetByReferenceType(string e) => e + "&";
        public string GetFunctionPointerType(MethodSignature<string> s) => "fnptr";
        public string GetGenericInstantiation(string g, ImmutableArray<string> a) => g + "<" + string.Join(",", a) + ">";
        public string GetGenericMethodParameter(object c, int i) => "!!" + i;
        public string GetGenericTypeParameter(object c, int i) => "!" + i;
        public string GetModifiedType(string m, string u, bool isRequired) => u;
        public string GetPinnedType(string e) => e;
        public string GetPointerType(string e) => e + "*";
        public string GetPrimitiveType(PrimitiveTypeCode c) => c switch
        {
            PrimitiveTypeCode.Boolean => "bool", PrimitiveTypeCode.Byte => "byte",
            PrimitiveTypeCode.SByte => "sbyte", PrimitiveTypeCode.Char => "char",
            PrimitiveTypeCode.Int16 => "short", PrimitiveTypeCode.UInt16 => "ushort",
            PrimitiveTypeCode.Int32 => "int", PrimitiveTypeCode.UInt32 => "uint",
            PrimitiveTypeCode.Int64 => "long", PrimitiveTypeCode.UInt64 => "ulong",
            PrimitiveTypeCode.Single => "float", PrimitiveTypeCode.Double => "double",
            PrimitiveTypeCode.IntPtr => "nint", PrimitiveTypeCode.UIntPtr => "nuint",
            PrimitiveTypeCode.Object => "object", PrimitiveTypeCode.String => "string",
            PrimitiveTypeCode.Void => "void", PrimitiveTypeCode.TypedReference => "typedref",
            _ => c.ToString(),
        };
        public string GetSZArrayType(string e) => e + "[]";
        public string GetTypeFromDefinition(MetadataReader r, TypeDefinitionHandle h, byte rawKind) => FullName(r, r.GetTypeDefinition(h));
        public string GetTypeFromReference(MetadataReader r, TypeReferenceHandle h, byte rawKind)
        {
            var tr = r.GetTypeReference(h);
            string ns = r.GetString(tr.Namespace);
            return (string.IsNullOrEmpty(ns) ? "" : ns + ".") + r.GetString(tr.Name);
        }
        public string GetTypeFromSpecification(MetadataReader r, object c, TypeSpecificationHandle h, byte rawKind)
            => r.GetTypeSpecification(h).DecodeSignature(this, c);
    }
}
