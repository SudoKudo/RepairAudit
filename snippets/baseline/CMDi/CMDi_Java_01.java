import java.io.IOException;

public class CMDi_Java_01 {
    public int pingHost(String host) throws IOException, InterruptedException {
        Process proc = Runtime.getRuntime().exec("ping -c 1 " + host);
        return proc.waitFor();
    }
}
