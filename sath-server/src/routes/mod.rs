mod auth;
mod sync;
mod health;

use axum::Router;
use crate::models::AppState;

pub fn api_routes() -> Router<AppState> {
    Router::new()
        .merge(health::routes())
        .merge(auth::routes())
        .merge(sync::routes())
}
