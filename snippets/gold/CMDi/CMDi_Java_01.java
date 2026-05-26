import java.io.IOException;

public class CMDi_Java_01 {
    public int pingHost(String host) throws IOException, InterruptedException {
        Process proc = new ProcessBuilder("ping", "-c", "1", host).start();
        return proc.waitFor();
    }
}
