using System;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using ICSharpCode.Decompiler;
using ICSharpCode.Decompiler.CSharp;
using ICSharpCode.Decompiler.Metadata;
using ICSharpCode.Decompiler.TypeSystem;

/// <summary>
/// decomp — offline decompiler for CS2's managed assemblies.
///
/// Answers "what does CS2 actually do?" by handing back real C# instead of IL. It replaced
/// mdump (a System.Reflection.Metadata IL walker) after hand-reading IL produced two wrong
/// readings of `CalculateNodeParameters` that the decompiled source settled in minutes: IL
/// answers "what does this method call"; arithmetic, operand flow and compiler-generated
/// structure need source.
///
/// Modes:
///   decomp type <dllGlob> <FullTypeName>     one type as C#, to stdout
///   decomp list <dllGlob> <typeRegex>        matching full type names
///   decomp all  <dllGlob> <outDir>           every top-level type, one .cs per type — then grep
///
/// `all` + ripgrep covers mdump's `strings` and `find` modes: string literals and member
/// references both appear in the source. Nested types decompile with their parent.
/// </summary>
internal static class Program
{
    private static int Main(string[] args)
    {
        if (args.Length < 3)
        {
            Console.Error.WriteLine("usage: decomp type <dllGlob> <FullTypeName>\n"
                                  + "       decomp list <dllGlob> <typeRegex>\n"
                                  + "       decomp all  <dllGlob> <outDir>");
            return 2;
        }

        string managed = Environment.GetEnvironmentVariable("CSII_MANAGEDPATH")
            ?? @"C:\Program Files (x86)\Steam\steamapps\common\Cities Skylines II\Cities2_Data\Managed";

        string[] dlls = Directory.GetFiles(managed, args[1].EndsWith(".dll") ? args[1] : args[1] + ".dll");
        if (dlls.Length == 0)
        {
            Console.Error.WriteLine($"no assembly matches '{args[1]}' under {managed}");
            return 1;
        }

        foreach (string dll in dlls)
        {
            var decompiler = new CSharpDecompiler(dll, new DecompilerSettings
            {
                ThrowOnAssemblyResolveErrors = false,
                LoadInMemory = true
            });

            switch (args[0])
            {
                case "type":
                    Console.WriteLine(decompiler.DecompileTypeAsString(new FullTypeName(args[2])));
                    break;

                case "list":
                {
                    var rx = new Regex(args[2]);
                    foreach (var type in decompiler.TypeSystem.MainModule.TopLevelTypeDefinitions)
                        if (rx.IsMatch(type.FullName))
                            Console.WriteLine(type.FullName);
                    break;
                }

                case "all":
                {
                    string outDir = Path.Combine(args[2], Path.GetFileNameWithoutExtension(dll));
                    Directory.CreateDirectory(outDir);
                    int written = 0, failed = 0;

                    foreach (var type in decompiler.TypeSystem.MainModule.TopLevelTypeDefinitions.ToList())
                    {
                        // One type per file, named by its full name, so ripgrep hits carry the
                        // type in the path. A type that will not decompile is noted, not fatal.
                        string safe = new string(type.FullName.Select(c =>
                            char.IsLetterOrDigit(c) || c == '.' || c == '_' ? c : '_').ToArray());
                        try
                        {
                            File.WriteAllText(Path.Combine(outDir, safe + ".cs"),
                                              decompiler.DecompileTypeAsString(new FullTypeName(type.FullName)),
                                              Encoding.UTF8);
                            written++;
                        }
                        catch (Exception ex)
                        {
                            File.WriteAllText(Path.Combine(outDir, safe + ".FAILED.txt"), ex.ToString());
                            failed++;
                        }
                    }

                    Console.WriteLine($"{Path.GetFileName(dll)}: {written} type(s) -> {outDir}"
                                    + (failed > 0 ? $", {failed} failed" : ""));
                    break;
                }

                default:
                    Console.Error.WriteLine($"unknown mode '{args[0]}'");
                    return 2;
            }
        }

        return 0;
    }
}
