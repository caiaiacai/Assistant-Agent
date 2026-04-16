use axum::{
    extract::State,
    http::{HeaderMap, StatusCode},
    routing::post,
    Json, Router,
};
use uuid::Uuid;

use crate::models::{AppState, Claims, SyncPullResponse, SyncPushRequest, SyncTodoItem, Todo};

pub fn routes() -> Router<AppState> {
    Router::new().route("/sync", post(sync))
}

/// Extract and verify JWT from Authorization header.
fn extract_user_id(headers: &HeaderMap, jwt_secret: &str) -> Result<Uuid, StatusCode> {
    let auth = headers
        .get("Authorization")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    let token = auth.strip_prefix("Bearer ").ok_or(StatusCode::UNAUTHORIZED)?;

    let data = jsonwebtoken::decode::<Claims>(
        token,
        &jsonwebtoken::DecodingKey::from_secret(jwt_secret.as_bytes()),
        &jsonwebtoken::Validation::default(),
    )
    .map_err(|_| StatusCode::UNAUTHORIZED)?;

    data.claims
        .sub
        .parse()
        .map_err(|_| StatusCode::UNAUTHORIZED)
}

/// Sync endpoint: client pushes local changes, gets back remote changes.
/// Protocol: last-write-wins based on `updated_at`.
async fn sync(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(req): Json<SyncPushRequest>,
) -> Result<Json<SyncPullResponse>, (StatusCode, String)> {
    let user_id =
        extract_user_id(&headers, &state.jwt_secret).map_err(|s| (s, "Unauthorized".into()))?;
    let now = chrono::Utc::now();

    // 1. Upsert client changes (last-write-wins)
    for item in &req.todos {
        upsert_todo(&state, user_id, item)
            .await
            .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    }

    // 2. Pull server changes since client's last sync
    let since = req.last_sync_at.unwrap_or(chrono::DateTime::UNIX_EPOCH);
    let server_todos: Vec<Todo> = sqlx::query_as(
        "SELECT * FROM todos WHERE user_id = $1 AND updated_at > $2 ORDER BY updated_at ASC",
    )
    .bind(user_id)
    .bind(since)
    .fetch_all(&state.db)
    .await
    .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;

    Ok(Json(SyncPullResponse {
        todos: server_todos,
        server_time: now,
    }))
}

async fn upsert_todo(
    state: &AppState,
    user_id: Uuid,
    item: &SyncTodoItem,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        r#"
        INSERT INTO todos (id, user_id, content, title, status, types, tags,
                           priority, due_at, plan, agent_output, agent_status,
                           created_at, updated_at, deleted)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
        ON CONFLICT (id, user_id)
        DO UPDATE SET
            content      = CASE WHEN EXCLUDED.updated_at > todos.updated_at THEN EXCLUDED.content      ELSE todos.content END,
            title        = CASE WHEN EXCLUDED.updated_at > todos.updated_at THEN EXCLUDED.title        ELSE todos.title END,
            status       = CASE WHEN EXCLUDED.updated_at > todos.updated_at THEN EXCLUDED.status       ELSE todos.status END,
            types        = CASE WHEN EXCLUDED.updated_at > todos.updated_at THEN EXCLUDED.types        ELSE todos.types END,
            tags         = CASE WHEN EXCLUDED.updated_at > todos.updated_at THEN EXCLUDED.tags         ELSE todos.tags END,
            priority     = CASE WHEN EXCLUDED.updated_at > todos.updated_at THEN EXCLUDED.priority     ELSE todos.priority END,
            due_at       = CASE WHEN EXCLUDED.updated_at > todos.updated_at THEN EXCLUDED.due_at       ELSE todos.due_at END,
            plan         = CASE WHEN EXCLUDED.updated_at > todos.updated_at THEN EXCLUDED.plan         ELSE todos.plan END,
            agent_output = CASE WHEN EXCLUDED.updated_at > todos.updated_at THEN EXCLUDED.agent_output ELSE todos.agent_output END,
            agent_status = CASE WHEN EXCLUDED.updated_at > todos.updated_at THEN EXCLUDED.agent_status ELSE todos.agent_status END,
            updated_at   = CASE WHEN EXCLUDED.updated_at > todos.updated_at THEN EXCLUDED.updated_at   ELSE todos.updated_at END,
            deleted      = CASE WHEN EXCLUDED.updated_at > todos.updated_at THEN EXCLUDED.deleted      ELSE todos.deleted END
        "#,
    )
    .bind(&item.id)
    .bind(user_id)
    .bind(&item.content)
    .bind(&item.title)
    .bind(&item.status)
    .bind(&item.types)
    .bind(&item.tags)
    .bind(item.priority)
    .bind(item.due_at)
    .bind(&item.plan)
    .bind(&item.agent_output)
    .bind(&item.agent_status)
    .bind(item.created_at)
    .bind(item.updated_at)
    .bind(item.deleted)
    .execute(&state.db)
    .await?;

    Ok(())
}
