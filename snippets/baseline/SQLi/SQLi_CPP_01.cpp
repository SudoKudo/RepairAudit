#include <sqlite3.h>
#include <string>

int lookup_user(sqlite3 *db, const std::string& username) {
    std::string query = "SELECT id, username, role FROM users WHERE username = '" + username + "'";
    return sqlite3_exec(db, query.c_str(), nullptr, nullptr, nullptr);
}
