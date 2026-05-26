import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;

public class SQLi_Java_01 {
    public ResultSet getUserByUsername(Connection conn, String username) throws SQLException {
        String query = "SELECT id, username, role FROM users WHERE username = ?";
        PreparedStatement stmt = conn.prepareStatement(query);
        stmt.setString(1, username);
        return stmt.executeQuery();
    }
}
