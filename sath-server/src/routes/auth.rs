use axum::{
    extract::State,
    http::StatusCode,
    routing::post,
    Json, Router,
};
use jsonwebtoken::{encode, EncodingKey, Header};
use uuid::Uuid;

use crate::models::{
    AppState, AuthResponse, Claims, SendCodeRequest, User, VerifyCodeRequest,
};

pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/auth/send-code", post(send_code))
        .route("/auth/verify", post(verify_code))
}

/// Send SMS verification code to phone number.
async fn send_code(
    State(state): State<AppState>,
    Json(req): Json<SendCodeRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    // Validate phone format (Chinese mobile: 1xx-xxxx-xxxx)
    let phone = req.phone.trim();
    if phone.len() != 11 || !phone.starts_with('1') {
        return Err((StatusCode::BAD_REQUEST, "Invalid phone number".into()));
    }

    crate::models::sms::send_verify_code(&state.sms, phone)
        .await
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e))?;

    Ok(Json(serde_json::json!({ "status": "sent" })))
}

/// Verify SMS code and return JWT + user.
async fn verify_code(
    State(state): State<AppState>,
    Json(req): Json<VerifyCodeRequest>,
) -> Result<Json<AuthResponse>, (StatusCode, String)> {
    let phone = req.phone.trim();
    let code = req.code.trim();

    // Verify with Aliyun
    let valid = crate::models::sms::check_verify_code(&state.sms, phone, code)
        .await
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e))?;

    if !valid {
        return Err((StatusCode::UNAUTHORIZED, "Invalid code".into()));
    }

    // Find or create user
    let user = find_or_create_user(&state, phone).await
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;

    // Generate JWT (30 days)
    let exp = chrono::Utc::now()
        .checked_add_signed(chrono::Duration::days(30))
        .unwrap()
        .timestamp() as usize;

    let claims = Claims {
        sub: user.id.to_string(),
        exp,
    };

    let token = encode(
        &Header::default(),
        &claims,
        &EncodingKey::from_secret(state.jwt_secret.as_bytes()),
    )
    .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;

    Ok(Json(AuthResponse { token, user }))
}

async fn find_or_create_user(
    state: &AppState,
    phone: &str,
) -> Result<User, sqlx::Error> {
    // Try to find existing user
    let existing: Option<User> = sqlx::query_as(
        "SELECT * FROM users WHERE phone = $1"
    )
    .bind(phone)
    .fetch_optional(&state.db)
    .await?;

    if let Some(user) = existing {
        return Ok(user);
    }

    // Create new user
    let id = Uuid::new_v4();
    let now = chrono::Utc::now();
    let user: User = sqlx::query_as(
        "INSERT INTO users (id, phone, created_at, updated_at) \
         VALUES ($1, $2, $3, $4) RETURNING *"
    )
    .bind(id)
    .bind(phone)
    .bind(now)
    .bind(now)
    .fetch_one(&state.db)
    .await?;

    tracing::info!("New user created: {} ({})", user.id, phone);
    Ok(user)
}
