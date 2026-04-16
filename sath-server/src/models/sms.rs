use chrono::Utc;
use hmac::Mac;
use std::collections::BTreeMap;

/// Aliyun SMS Authentication Service config.
#[derive(Clone, Debug)]
pub struct SmsConfig {
    pub access_key_id: String,
    pub access_key_secret: String,
}

/// Call Aliyun DYPNS SendSmsVerifyCode API.
pub async fn send_verify_code(cfg: &SmsConfig, phone: &str) -> Result<String, String> {
    let action = "SendSmsVerifyCode";
    let now = Utc::now();
    let nonce = uuid::Uuid::new_v4().to_string();

    let mut params = BTreeMap::new();
    params.insert("Action", action.to_string());
    params.insert("Version", "2017-05-25".into());
    params.insert("Format", "JSON".into());
    params.insert("SignatureMethod", "HMAC-SHA1".into());
    params.insert("SignatureVersion", "1.0".into());
    params.insert("SignatureNonce", nonce);
    params.insert("Timestamp", now.format("%Y-%m-%dT%H:%M:%SZ").to_string());
    params.insert("AccessKeyId", cfg.access_key_id.clone());

    // SMS specific params
    params.insert("PhoneNumber", phone.to_string());
    params.insert("CodeLength", "6".into());
    params.insert("ValidTime", "300".into()); // 5 minutes

    // Build string to sign
    let query_parts: Vec<String> = params
        .iter()
        .map(|(k, v)| format!("{}={}", percent_encode(k), percent_encode(v)))
        .collect();
    let sorted_query = query_parts.join("&");
    let string_to_sign = format!("GET&{}&{}", percent_encode("/"), percent_encode(&sorted_query));

    // HMAC-SHA1 signature
    let sign_key = format!("{}&", cfg.access_key_secret);
    type HmacSha1 = hmac::Hmac<sha1::Sha1>;
    let mut mac =
        HmacSha1::new_from_slice(sign_key.as_bytes()).map_err(|e| e.to_string())?;
    mac.update(string_to_sign.as_bytes());
    let signature = base64::Engine::encode(
        &base64::engine::general_purpose::STANDARD,
        mac.finalize().into_bytes(),
    );

    let url = format!(
        "https://dypnsapi.aliyuncs.com/?{}&Signature={}",
        sorted_query,
        percent_encode(&signature)
    );

    let resp = reqwest::get(&url)
        .await
        .map_err(|e| format!("HTTP error: {e}"))?;
    let body: serde_json::Value = resp.json().await.map_err(|e| format!("Parse error: {e}"))?;

    tracing::info!("SendSmsVerifyCode response: {body}");

    if body["Code"].as_str() == Some("OK") {
        Ok("sent".into())
    } else {
        Err(format!(
            "SMS send failed: {}",
            body["Message"].as_str().unwrap_or("unknown")
        ))
    }
}

/// Call Aliyun DYPNS CheckSmsVerifyCode API.
pub async fn check_verify_code(
    cfg: &SmsConfig,
    phone: &str,
    code: &str,
) -> Result<bool, String> {
    let action = "CheckSmsVerifyCode";
    let now = Utc::now();
    let nonce = uuid::Uuid::new_v4().to_string();

    let mut params = BTreeMap::new();
    params.insert("Action", action.to_string());
    params.insert("Version", "2017-05-25".into());
    params.insert("Format", "JSON".into());
    params.insert("SignatureMethod", "HMAC-SHA1".into());
    params.insert("SignatureVersion", "1.0".into());
    params.insert("SignatureNonce", nonce);
    params.insert("Timestamp", now.format("%Y-%m-%dT%H:%M:%SZ").to_string());
    params.insert("AccessKeyId", cfg.access_key_id.clone());

    params.insert("PhoneNumber", phone.to_string());
    params.insert("VerifyCode", code.to_string());

    let query_parts: Vec<String> = params
        .iter()
        .map(|(k, v)| format!("{}={}", percent_encode(k), percent_encode(v)))
        .collect();
    let sorted_query = query_parts.join("&");
    let string_to_sign = format!("GET&{}&{}", percent_encode("/"), percent_encode(&sorted_query));

    let sign_key = format!("{}&", cfg.access_key_secret);
    type HmacSha1 = hmac::Hmac<sha1::Sha1>;
    let mut mac =
        HmacSha1::new_from_slice(sign_key.as_bytes()).map_err(|e| e.to_string())?;
    mac.update(string_to_sign.as_bytes());
    let signature = base64::Engine::encode(
        &base64::engine::general_purpose::STANDARD,
        mac.finalize().into_bytes(),
    );

    let url = format!(
        "https://dypnsapi.aliyuncs.com/?{}&Signature={}",
        sorted_query,
        percent_encode(&signature)
    );

    let resp = reqwest::get(&url)
        .await
        .map_err(|e| format!("HTTP error: {e}"))?;
    let body: serde_json::Value = resp.json().await.map_err(|e| format!("Parse error: {e}"))?;

    tracing::info!("CheckSmsVerifyCode response: {body}");

    if body["Code"].as_str() == Some("OK") {
        Ok(body["VerifyResult"].as_str() == Some("PASS"))
    } else {
        Err(format!(
            "SMS check failed: {}",
            body["Message"].as_str().unwrap_or("unknown")
        ))
    }
}

fn percent_encode(s: &str) -> String {
    let mut result = String::new();
    for byte in s.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                result.push(byte as char);
            }
            _ => {
                result.push_str(&format!("%{:02X}", byte));
            }
        }
    }
    result
}
