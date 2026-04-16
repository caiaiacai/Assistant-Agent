pub mod sms;

use sqlx::PgPool;

/// Shared app state available to all route handlers.
#[derive(Clone)]
pub struct AppState {
    pub db: PgPool,
    pub jwt_secret: String,
    pub sms: sms::SmsConfig,
}

// ---- User ----
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, sqlx::FromRow)]
pub struct User {
    pub id: uuid::Uuid,
    pub phone: String,
    pub nickname: Option<String>,
    pub avatar_url: Option<String>,
    pub created_at: chrono::DateTime<chrono::Utc>,
    pub updated_at: chrono::DateTime<chrono::Utc>,
}

// ---- Todo (synced) ----
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, sqlx::FromRow)]
pub struct Todo {
    pub id: String,               // client-generated UUID
    pub user_id: uuid::Uuid,
    pub content: String,
    pub title: Option<String>,
    pub status: String,           // pending | done
    pub types: serde_json::Value, // JSON array
    pub tags: serde_json::Value,  // JSON array
    pub priority: Option<i32>,
    pub due_at: Option<chrono::DateTime<chrono::Utc>>,
    pub plan: Option<serde_json::Value>,
    pub agent_output: Option<String>,
    pub agent_status: Option<String>,
    pub created_at: chrono::DateTime<chrono::Utc>,
    pub updated_at: chrono::DateTime<chrono::Utc>,
    pub deleted: bool,
}

// ---- Sync DTOs ----
#[derive(Debug, serde::Deserialize)]
pub struct SyncPushRequest {
    pub todos: Vec<SyncTodoItem>,
    pub last_sync_at: Option<chrono::DateTime<chrono::Utc>>,
}

#[derive(Debug, serde::Serialize)]
pub struct SyncPullResponse {
    pub todos: Vec<Todo>,
    pub server_time: chrono::DateTime<chrono::Utc>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct SyncTodoItem {
    pub id: String,
    pub content: String,
    pub title: Option<String>,
    pub status: String,
    pub types: serde_json::Value,
    pub tags: serde_json::Value,
    pub priority: Option<i32>,
    pub due_at: Option<chrono::DateTime<chrono::Utc>>,
    pub plan: Option<serde_json::Value>,
    pub agent_output: Option<String>,
    pub agent_status: Option<String>,
    pub created_at: chrono::DateTime<chrono::Utc>,
    pub updated_at: chrono::DateTime<chrono::Utc>,
    pub deleted: bool,
}

// ---- Auth DTOs ----
#[derive(Debug, serde::Deserialize)]
pub struct SendCodeRequest {
    pub phone: String,
}

#[derive(Debug, serde::Deserialize)]
pub struct VerifyCodeRequest {
    pub phone: String,
    pub code: String,
}

#[derive(Debug, serde::Serialize)]
pub struct AuthResponse {
    pub token: String,
    pub user: User,
}

// ---- JWT Claims ----
#[derive(Debug, serde::Serialize, serde::Deserialize)]
pub struct Claims {
    pub sub: String, // user_id
    pub exp: usize,
}
