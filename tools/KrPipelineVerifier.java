import java.io.File;
import kr.compile.KrDiagnostic;
import kr.compile.KrPipeline;

/**
 * Small non-interactive entry point for the shipped KR compiler.
 * It validates and generates the KR language boundary into the workspace build
 * directory without starting the Swing IDE.
 */
public final class KrPipelineVerifier {
    private KrPipelineVerifier() {}

    public static void main(String[] args) throws Exception {
        if (args.length != 1) {
            System.err.println("Usage: KrPipelineVerifier <project-directory>");
            System.exit(64);
        }
        File project = new File(args[0]).getCanonicalFile();
        File output = new File(project, "KR_BUILD/kr-generated");
        if (!output.isDirectory() && !output.mkdirs()) {
            throw new IllegalStateException("KR output directory could not be created: " + output);
        }
        KrPipeline.PipelineResult result = KrPipeline.process(project, output, false);
        for (KrDiagnostic diagnostic : result.diagnostics) {
            System.out.println(diagnostic);
        }
        if (!result.success) {
            System.err.println("KR pipeline validation failed.");
            System.exit(2);
        }
        System.out.println("KR pipeline verified: " + result.languagesUsed + ", generated at " + output);
    }
}
