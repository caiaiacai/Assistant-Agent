use axum::{routing::get, Json, Router};
use crate::models::AppState;

pub fn routes() -> Router<AppState> {
    Router::new().route("/health", get(health))
}

async fn health() -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "status": "ok",
        "service": "sath-server",
        "version": env!("CARGO_PKG_VERSION")
    }))
}
