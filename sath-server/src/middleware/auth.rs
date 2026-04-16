use axum::{
    extract::Request,
    http::StatusCode,
    middleware::Next,
    response::Response,
};
use jsonwebtoken::{decode, DecodingKey, Validation};

use crate::models::Claims;

/// Extract user_id from JWT in Authorization header.
/// Inserts user_id as a request extension so handlers can access it.
pub async fn require_auth(mut req: Request, next: Next) -> Result<Response, StatusCode> {
    let jwt_secret = req
        .extensions()
        .get::<String>()
        .cloned()
        .unwrap_or_default();

    let auth_header = req
        .headers()
        .get("Authorization")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");

    let token = auth_header
        .strip_prefix("Bearer ")
        .ok_or(StatusCode::UNAUTHORIZED)?;

    let token_data = decode::<Claims>(
        token,
        &DecodingKey::from_secret(jwt_secret.as_bytes()),
        &Validation::default(),
    )
    .map_err(|_| StatusCode::UNAUTHORIZED)?;

    let user_id: uuid::Uuid = token_data
        .claims
        .sub
        .parse()
        .map_err(|_| StatusCode::UNAUTHORIZED)?;

    req.extensions_mut().insert(user_id);
    Ok(next.run(req).await)
}
