mod models;
mod routes;
mod middleware;

use axum::Router;
use sqlx::postgres::PgPoolOptions;
use tower_http::cors::{CorsLayer, Any};
use tower_http::trace::TraceLayer;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() {
    dotenvy::dotenv().ok();

    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .init();

    let database_url = std::env::var("DATABASE_URL").expect("DATABASE_URL must be set");
    let pool = PgPoolOptions::new()
        .max_connections(20)
        .connect(&database_url)
        .await
        .expect("Failed to connect to database");

    sqlx::migrate!("./migrations")
        .run(&pool)
        .await
        .expect("Failed to run migrations");
    tracing::info!("Migrations applied");

    let jwt_secret = std::env::var("JWT_SECRET").expect("JWT_SECRET must be set");

    let sms_config = models::sms::SmsConfig {
        access_key_id: std::env::var("ALIYUN_ACCESS_KEY_ID").unwrap_or_default(),
        access_key_secret: std::env::var("ALIYUN_ACCESS_KEY_SECRET").unwrap_or_default(),
    };

    let state = models::AppState {
        db: pool,
        jwt_secret,
        sms: sms_config,
    };

    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods(Any)
        .allow_headers(Any);

    let app = Router::new()
        .nest("/api", routes::api_routes())
        .layer(cors)
        .layer(TraceLayer::new_for_http())
        .with_state(state);

    let addr = "0.0.0.0:3000";
    tracing::info!("SATH server listening on {addr}");
    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
