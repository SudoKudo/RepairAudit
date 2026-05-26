#include <sqlite3.h>
#include <string>

int lookup_user(sqlite3 *db, const std::string& username) {
    sqlite3_stmt *stmt = nullptr;
    const char *query = "SELECT id, username, role FROM users WHERE username = ?";
    if (sqlite3_prepare_v2(db, query, -1, &stmt, nullptr) != SQLITE_OK) {
        return SQLITE_ERROR;
    }

    sqlite3_bind_text(stmt, 1, username.c_str(), -1, SQLITE_TRANSIENT);
    int rc = sqlite3_step(stmt);
    sqlite3_finalize(stmt);
    return rc;
}
