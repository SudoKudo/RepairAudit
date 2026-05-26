import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;

public class SQLi_Java_01 {
    public ResultSet getUserByUsername(Connection conn, String username) throws SQLException {
        Statement stmt = conn.createStatement();
        String query = "SELECT id, username, role FROM users WHERE username = '" + username + "'";
        return stmt.executeQuery(query);
    }
}
