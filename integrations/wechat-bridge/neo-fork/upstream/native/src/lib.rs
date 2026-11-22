use anyhow::{anyhow, ensure, Context, Result};
use base64::{engine::general_purpose::STANDARD, Engine};
use napi::{bindgen_prelude::AsyncTask, Env, Task};
use napi_derive::napi;
use serde::Serialize;
use serde_json::{json, Value};
use std::{
    sync::{Arc, Mutex},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use wechat_protocol::{
    is_session_rejected, wire, AccountInterrupted, AccountPushConnection, DeviceProfile,
    LoginAttempt, LoginStatus, NativeClient, NativeOptions, Service, ServiceRejected,
    TextSendOptions,
};

const DEVICE_NAME: &str = "Puppet Neo";
/// Error text TypeScript receives when WeChat no longer accepts this session.
const SESSION_ENDED: &str = "Neo session ended by WeChat";

/// WeChat rejected the session's credentials and rejected one renewal.
#[derive(Debug)]
struct SessionEnded;

impl std::fmt::Display for SessionEnded {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(SESSION_ENDED)
    }
}

impl std::error::Error for SessionEnded {}

#[derive(Default)]
struct Session {
    attempt: Option<LoginAttempt>,
    /// Owns the accepted credentials after login; account requests share it.
    account: Option<NativeClient>,
    incoming: Option<AccountPushConnection>,
    received_cursor: Vec<u8>,
}

#[derive(Debug, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
struct TextMessage {
    id: String,
    /// Who wrote the message; in a group, the member who posted it.
    talker: String,
    /// The account that received a direct message.
    #[serde(skip_serializing_if = "Option::is_none")]
    listener: Option<String>,
    /// The group the message was posted in.
    #[serde(skip_serializing_if = "Option::is_none")]
    room: Option<String>,
    text: String,
    timestamp: u32,
}

#[derive(Default, Serialize)]
struct SelfProfile {
    nickname: Option<String>,
    alias: Option<String>,
    avatar: Option<String>,
}

enum Operation {
    StartLogin,
    PollLogin,
    Heartbeat,
    PollIncoming,
    Sync { scene: u32 },
    SendText { destination: String, text: String },
    LookupSelf,
    Logout,
    Close,
}

#[napi]
#[derive(Default)]
pub struct NeoSession {
    session: Arc<Mutex<Session>>,
}

#[napi]
impl NeoSession {
    #[napi(constructor)]
    pub fn new() -> Self {
        Self::default()
    }

    #[napi]
    pub fn start_login(&self) -> AsyncTask<NeoTask> {
        self.task(Operation::StartLogin)
    }

    #[napi]
    pub fn poll_login(&self) -> AsyncTask<NeoTask> {
        self.task(Operation::PollLogin)
    }

    #[napi]
    pub fn heartbeat(&self) -> AsyncTask<NeoTask> {
        self.task(Operation::Heartbeat)
    }

    #[napi]
    pub fn sync(&self, scene: u32) -> AsyncTask<NeoTask> {
        self.task(Operation::Sync { scene })
    }

    #[napi]
    pub fn poll_incoming(&self) -> AsyncTask<NeoTask> {
        self.task(Operation::PollIncoming)
    }

    #[napi]
    pub fn send_text(&self, destination: String, text: String) -> AsyncTask<NeoTask> {
        self.task(Operation::SendText { destination, text })
    }

    #[napi]
    pub fn lookup_self(&self) -> AsyncTask<NeoTask> {
        self.task(Operation::LookupSelf)
    }

    #[napi]
    pub fn logout(&self) -> AsyncTask<NeoTask> {
        self.task(Operation::Logout)
    }

    #[napi]
    pub fn close(&self) -> AsyncTask<NeoTask> {
        self.task(Operation::Close)
    }
}

impl NeoSession {
    fn task(&self, operation: Operation) -> AsyncTask<NeoTask> {
        AsyncTask::new(NeoTask {
            session: self.session.clone(),
            operation: Some(operation),
        })
    }
}

pub struct NeoTask {
    session: Arc<Mutex<Session>>,
    operation: Option<Operation>,
}

impl Task for NeoTask {
    type Output = Value;
    type JsValue = String;

    fn compute(&mut self) -> napi::Result<Self::Output> {
        let mut session = self
            .session
            .lock()
            .map_err(|_| napi::Error::from_reason("Neo session unavailable"))?;
        let operation = self
            .operation
            .take()
            .ok_or_else(|| napi::Error::from_reason("Operation already completed"))?;
        let stage = match &operation {
            Operation::StartLogin => "start QR login",
            Operation::PollLogin => "check or complete QR login",
            Operation::Heartbeat => "check session heartbeat",
            Operation::PollIncoming => "wait for incoming message notifications",
            Operation::Sync { .. } => "read a sync page",
            Operation::SendText { .. } => "prepare a text send",
            Operation::LookupSelf => "look up the account profile",
            Operation::Logout => "log out",
            Operation::Close => "close the session",
        };
        run(&mut session, operation)
            .map_err(|error| napi::Error::from_reason(failure_message(stage, &error)))
    }

    fn resolve(&mut self, _env: Env, output: Self::Output) -> napi::Result<Self::JsValue> {
        Ok(output.to_string())
    }
}

/// The error text TypeScript receives. When WeChat answered, it adds the service
/// and its code, or why the account stopped. It never contains credentials or
/// message content.
fn failure_message(stage: &str, error: &anyhow::Error) -> String {
    if error.is::<SessionEnded>() {
        SESSION_ENDED.into()
    } else if let Some(rejected) = ServiceRejected::find(error) {
        format!("Neo could not {stage}: {rejected}")
    } else if let Some(reason) = AccountInterrupted::find(error) {
        format!("Neo could not {stage}: {reason}")
    } else {
        format!("Neo could not {stage}")
    }
}

fn options() -> NativeOptions {
    NativeOptions {
        timeout: Duration::from_secs(10),
        ..Default::default()
    }
}

fn run(session: &mut Session, operation: Operation) -> Result<Value> {
    match operation {
        Operation::StartLogin => {
            ensure!(
                session.account.is_none() && session.attempt.is_none(),
                "Login already started"
            );
            let device = DeviceProfile::generate("UnifiedPCLinux x86_64".into())?;
            let attempt = NativeClient::new(options())?.start_qr_login(device, DEVICE_NAME)?;
            let output = json!({ "png": STANDARD.encode(&attempt.qr_png) });
            session.attempt = Some(attempt);
            Ok(output)
        }
        Operation::PollLogin => {
            let attempt = session
                .attempt
                .as_mut()
                .ok_or_else(|| anyhow!("No login attempt"))?;
            let client = NativeClient::new(options())?;
            let status = match client.poll_login(attempt) {
                Ok(status) => status,
                Err(error) if wechat_protocol::is_retryable_login_poll_error(&error) => {
                    return Ok(json!({ "status": "retry" }))
                }
                Err(error) => return Err(error),
            };
            if status == LoginStatus::Approved {
                let mut attempt = session.attempt.take().expect("issued challenge");
                let state = client.complete_login(&mut attempt)?;
                let result = json!({ "status": "completed", "username": state.own_username });
                session.account = Some(NativeClient::from_account(state, options())?);
                return Ok(result);
            }
            Ok(json!({ "status": status }))
        }
        Operation::Heartbeat => {
            let beat = read_session(session, heartbeat)?;
            Ok(
                json!({ "sync": beat.selector & 7 != 0, "intervalMs": beat.next_interval.clamp(30, 180) * 1000 }),
            )
        }
        Operation::PollIncoming => {
            let connection = session
                .incoming
                .as_mut()
                .context("No incoming connection")?;
            match connection.poll(Duration::from_secs(1)) {
                Ok(notification) => Ok(json!(
                    notification.is_some_and(|hint| hint.command == 24 && hint.selector & 7 != 0)
                )),
                Err(error) if account_ended(&error) => Err(error),
                Err(_) => {
                    // The notification socket closed. Reconnect and let one
                    // heartbeat decide whether WeChat still accepts the session.
                    session.incoming = None;
                    let beat = read_session(session, heartbeat)?;
                    Ok(json!(beat.selector & 7 != 0))
                }
            }
        }
        Operation::Sync { scene } => read_session(session, |session| sync(session, scene)),
        Operation::SendText { destination, text } => {
            let account = session
                .account
                .as_ref()
                .ok_or_else(|| anyhow!("No active session"))?;
            let created_at =
                u32::try_from(SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs())?;
            let mut random = [0; 4];
            openssl::rand::rand_bytes(&mut random)?;
            let client_id = u32::from_le_bytes(random).max(1);
            // Validate locally before the operation is allowed to reach transport.
            wire::text_send_request(&destination, &text, created_at, client_id)?;
            let send = TextSendOptions {
                created_at,
                client_message_id: client_id,
                source: None,
            };
            Ok(submit_once(|| {
                account.request_blocking(|request| request.send_text(&destination, &text, &send))
            }))
        }
        Operation::LookupSelf => lookup_self(session),
        Operation::Logout => {
            session.incoming = None;
            let account = session.account.take().context("No active session")?;
            // Reserving the logout stops new account requests before submission.
            let reply = account.prepare_logout()?.submit_blocking()?;
            Ok(json!({ "accepted": reply.accepted }))
        }
        Operation::Close => {
            if let Some(account) = session.account.take() {
                account.stop()?;
            }
            *session = Session::default();
            Ok(Value::Null)
        }
    }
}

fn connection(session: &mut Session) -> Result<&mut AccountPushConnection> {
    if session.incoming.is_none() {
        let account = session.account.as_ref().context("No active session")?;
        session.incoming = Some(account.open_push_connection_blocking()?);
    }
    session.incoming.as_mut().context("No incoming connection")
}

fn heartbeat(session: &mut Session) -> Result<wire::Heartbeat> {
    let cursor = session.received_cursor.clone();
    connection(session)?.heartbeat(&cursor)
}

fn sync(session: &mut Session, scene: u32) -> Result<Value> {
    let own = session
        .account
        .as_ref()
        .context("No active session")?
        .account_identity()?
        .own_username;
    let cursor = session.received_cursor.clone();
    let batch = connection(session)?.sync(&cursor, scene)?;
    let messages = text_messages(batch.commands, &own);
    session.received_cursor = batch.cursor;
    Ok(json!({ "messages": messages, "continuation": batch.continuation }))
}

/// Read-only session work. Sends never use this.
fn read_session<T>(
    session: &mut Session,
    operation: impl FnMut(&mut Session) -> Result<T>,
) -> Result<T> {
    recover(
        session,
        operation,
        |session| {
            session
                .account
                .as_ref()
                .context("No active session")?
                .renew_session_blocking(DEVICE_NAME)
        },
        // A new socket is needed after a failure and after a renewal, because a
        // socket cannot adopt newer credentials.
        |session| session.incoming = None,
    )
}

/// Repeat read-only work after recovering from one socket failure and one
/// credential rejection. A socket failure reconnects, so the next attempt shows
/// whether WeChat still accepts the session; signing the device out from the
/// phone can close the socket before any service code arrives. A rejection
/// renews the credentials once, and a rejected renewal or a second rejection
/// ends the session. Other service codes and a stopped account are returned.
fn recover<S, T>(
    session: &mut S,
    mut operation: impl FnMut(&mut S) -> Result<T>,
    mut renew: impl FnMut(&mut S) -> Result<()>,
    reconnect: impl Fn(&mut S),
) -> Result<T> {
    let mut reconnected = false;
    let mut renewed = false;
    loop {
        let error = match operation(session) {
            Ok(value) => return Ok(value),
            Err(error) => error,
        };
        if is_session_rejected(&error) {
            if renewed {
                return Err(error.context(SessionEnded));
            }
            renewed = true;
            if let Err(renewal) = renew(session) {
                return Err(if renewal_rejected(&renewal) {
                    renewal.context(SessionEnded)
                } else {
                    renewal
                });
            }
            reconnect(session);
        } else if !reconnected && reconnectable(&error) {
            reconnected = true;
            reconnect(session);
        } else {
            return Err(error);
        }
    }
}

/// The account was stopped or handed off here, so reconnecting cannot help.
fn account_ended(error: &anyhow::Error) -> bool {
    matches!(
        AccountInterrupted::find(error),
        Some(AccountInterrupted::Stopped | AccountInterrupted::Transferred)
    )
}

/// No service code came back, so the socket may be gone.
fn reconnectable(error: &anyhow::Error) -> bool {
    ServiceRejected::find(error).is_none() && !account_ended(error)
}

fn renewal_rejected(error: &anyhow::Error) -> bool {
    is_session_rejected(error)
        || ServiceRejected::find(error)
            .is_some_and(|rejected| rejected.service == Service::AutomaticAuthentication)
}

fn lookup_self(session: &mut Session) -> Result<Value> {
    let account = session.account.as_ref().context("No active session")?;
    let own = account.account_identity()?.own_username;
    let lookup =
        account.request_blocking(|request| request.lookup_contacts(std::slice::from_ref(&own)))?;
    let present = |value: Option<String>| value.filter(|text| !text.is_empty());
    let profile = lookup
        .contacts
        .into_iter()
        .find(|contact| contact.username == own)
        .map(|contact| SelfProfile {
            nickname: present(contact.nickname),
            alias: present(contact.alias),
            avatar: present(contact.large_avatar_url).or(present(contact.small_avatar_url)),
        })
        .unwrap_or_default();
    Ok(json!(profile))
}

fn submit_once(send: impl FnOnce() -> Result<wire::TextSendResult>) -> Value {
    match send() {
        Ok(result)
            if result.accepted
                && result.returned_destination_matches
                && result.returned_client_id_matches =>
        {
            json!({ "status": "accepted", "serverMessageId": result.server_message_id })
        }
        Ok(result)
            if !result.accepted
                && (result.outer_code != 0
                    || result.returned_destination_matches
                        && result.returned_client_id_matches
                        && result.item_code.is_some_and(|code| code != 0)) =>
        {
            json!({ "status": "rejected" })
        }
        _ => json!({ "status": "uncertain" }),
    }
}

fn text_messages(commands: Vec<wire::SyncCommand>, username: &str) -> Vec<TextMessage> {
    commands
        .into_iter()
        .filter(|command| command.id == 5)
        .filter_map(|command| wire::decode_add_message(&command.payload, username).ok())
        .filter_map(incoming_text)
        .collect()
}

/// Incoming text with a server message ID. A group message names its member
/// sender in the content; one without a valid sender is skipped, never credited
/// to the group.
fn incoming_text(message: wire::AddMessage) -> Option<TextMessage> {
    if message.message_type != 1 || message.outgoing {
        return None;
    }
    let (id, text) = (message.server_message_id?, message.content_text?);
    let timestamp = message.created_at;
    if message.from.ends_with("@chatroom") {
        let (sender, body) = group_sender(&text)?;
        Some(TextMessage {
            id,
            talker: sender.into(),
            listener: None,
            room: Some(message.from),
            text: body.into(),
            timestamp,
        })
    } else {
        Some(TextMessage {
            id,
            talker: message.from,
            listener: Some(message.to),
            room: None,
            text,
            timestamp,
        })
    }
}

/// Split group content into its sender and text. As in agent-wechat's daemon,
/// the sender is a plain identity of at most 512 bytes before the first `:\n`,
/// and never a group.
fn group_sender(text: &str) -> Option<(&str, &str)> {
    let (sender, body) = text.split_once(":\n")?;
    let valid = !sender.is_empty()
        && sender.len() <= 512
        && !sender.chars().any(char::is_control)
        && !sender.ends_with("@chatroom");
    valid.then_some((sender, body))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lost_send_response_is_uncertain_after_one_attempt() {
        let mut attempts = 0;
        let result = submit_once(|| {
            attempts += 1;
            Err(anyhow!("lost acknowledgment"))
        });
        assert_eq!(attempts, 1);
        assert_eq!(result["status"], "uncertain");
    }

    fn rejected(service: Service, code: i32) -> anyhow::Error {
        anyhow::Error::new(ServiceRejected::new(service, code))
    }

    /// Run `recover` over scripted attempts and renewals, recording each step.
    fn scripted_recovery(
        attempts: Vec<Result<&'static str>>,
        renewal: Result<()>,
    ) -> (Result<&'static str>, Vec<&'static str>) {
        let mut attempts = attempts.into_iter();
        let mut renewal = Some(renewal);
        let mut steps = vec![];
        let result = recover(
            &mut steps,
            |steps| {
                steps.push("attempt");
                attempts.next().expect("an attempt was scripted")
            },
            |steps| {
                steps.push("renew");
                renewal.take().expect("one renewal was scripted")
            },
            |steps| steps.push("reconnect"),
        );
        (result, steps)
    }

    #[test]
    fn a_socket_closed_by_phone_sign_out_ends_the_session() {
        // Signing the device out from the phone closed the socket during sync. The
        // reconnected sync is rejected, and so is the renewal.
        let (result, steps) = scripted_recovery(
            vec![
                Err(anyhow!("connection closed").context("read a sync page")),
                Err(rejected(Service::Sync, -13)),
            ],
            Err(rejected(Service::AutomaticAuthentication, -100)),
        );
        let error = result.unwrap_err();
        assert!(error.is::<SessionEnded>());
        assert_eq!(failure_message("read a sync page", &error), SESSION_ENDED);
        assert_eq!(steps, ["attempt", "reconnect", "attempt", "renew"]);
    }

    #[test]
    fn a_reconnect_or_a_renewal_lets_read_only_work_continue() {
        let (result, steps) =
            scripted_recovery(vec![Err(anyhow!("connection reset")), Ok("page")], Ok(()));
        assert_eq!(result.unwrap(), "page");
        assert_eq!(steps, ["attempt", "reconnect", "attempt"]);

        let (result, steps) = scripted_recovery(
            vec![Err(rejected(Service::Heartbeat, -13)), Ok("page")],
            Ok(()),
        );
        assert_eq!(result.unwrap(), "page");
        assert_eq!(steps, ["attempt", "renew", "reconnect", "attempt"]);
    }

    #[test]
    fn a_rejection_after_renewal_ends_the_session() {
        let (result, steps) = scripted_recovery(
            vec![
                Err(rejected(Service::NativeEnvelope, -13)),
                Err(rejected(Service::Sync, -13)),
            ],
            Ok(()),
        );
        assert!(result.unwrap_err().is::<SessionEnded>());
        assert_eq!(steps, ["attempt", "renew", "reconnect", "attempt"]);
    }

    #[test]
    fn a_failed_renewal_ends_the_session_only_when_wechat_rejects_it() {
        let (result, _) = scripted_recovery(
            vec![Err(rejected(Service::Sync, -13))],
            Err(rejected(Service::NativeEnvelope, -13)),
        );
        assert!(result.unwrap_err().is::<SessionEnded>());

        let (result, _) = scripted_recovery(
            vec![Err(rejected(Service::Sync, -13))],
            Err(anyhow!("connection reset")),
        );
        assert!(!result.unwrap_err().is::<SessionEnded>());
    }

    #[test]
    fn other_codes_a_stopped_account_and_a_second_socket_failure_are_returned() {
        let (result, steps) = scripted_recovery(vec![Err(rejected(Service::Sync, -1))], Ok(()));
        let error = result.unwrap_err();
        assert!(!error.is::<SessionEnded>());
        assert_eq!(
            failure_message("read a sync page", &error),
            "Neo could not read a sync page: Sync service returned -1"
        );
        assert_eq!(steps, ["attempt"]);

        let stopped = anyhow::Error::new(AccountInterrupted::Stopped).context("Account is stopped");
        let (result, steps) = scripted_recovery(vec![Err(stopped)], Ok(()));
        assert_eq!(
            failure_message("read a sync page", &result.unwrap_err()),
            "Neo could not read a sync page: Account stopped"
        );
        assert_eq!(steps, ["attempt"]);

        let (result, steps) = scripted_recovery(
            vec![
                Err(anyhow!("connection reset")),
                Err(anyhow!("connection reset")),
            ],
            Ok(()),
        );
        assert_eq!(
            failure_message("read a sync page", &result.unwrap_err()),
            "Neo could not read a sync page"
        );
        assert_eq!(steps, ["attempt", "reconnect", "attempt"]);
    }

    #[test]
    fn only_typed_session_rejections_count() {
        // The crate's typed rejection decides; matching text alone never ends a session.
        assert!(!is_session_rejected(&anyhow!("Sync service returned -13")));
        assert!(!is_session_rejected(&rejected(Service::Sync, -130)));
        assert!(!is_session_rejected(&rejected(Service::CdnDownload, -13)));
        assert!(renewal_rejected(&rejected(
            Service::AutomaticAuthentication,
            -100
        )));
        assert!(!renewal_rejected(&rejected(Service::Avatar, -100)));
        assert!(!renewal_rejected(&anyhow!("connection reset")));
    }

    fn add_message(from: &str, to: &str, text: &str) -> wire::AddMessage {
        wire::AddMessage {
            from: from.into(),
            to: to.into(),
            peer: if to == "fixture-self" { from } else { to }.into(),
            sender_id: (!from.ends_with("@chatroom")).then(|| from.into()),
            outgoing: from == "fixture-self",
            server_message_id: Some("fixture-message".into()),
            numbered1: 0,
            message_type: 1,
            content: text.as_bytes().to_vec(),
            content_text: Some(text.into()),
            numbered6: 0,
            numbered7: 0,
            inline_data: vec![],
            created_at: 1700000000,
            msgsource: None,
            opaque11: None,
            numbered13: None,
            server_sequence: None,
            raw: vec![],
        }
    }

    #[test]
    fn direct_and_group_text_name_the_talker() {
        assert_eq!(
            incoming_text(add_message("fixture-peer", "fixture-self", "ding")),
            Some(TextMessage {
                id: "fixture-message".into(),
                talker: "fixture-peer".into(),
                listener: Some("fixture-self".into()),
                room: None,
                text: "ding".into(),
                timestamp: 1700000000,
            })
        );
        assert_eq!(
            incoming_text(add_message(
                "fixture-room@chatroom",
                "fixture-self",
                "fixture-member:\nding"
            )),
            Some(TextMessage {
                id: "fixture-message".into(),
                talker: "fixture-member".into(),
                listener: None,
                room: Some("fixture-room@chatroom".into()),
                text: "ding".into(),
                timestamp: 1700000000,
            })
        );
        let json = serde_json::to_value(
            incoming_text(add_message(
                "fixture-room@chatroom",
                "fixture-self",
                "fixture-member:\nline one\nline two",
            ))
            .unwrap(),
        )
        .unwrap();
        assert_eq!(json["text"], "line one\nline two");
        assert!(json.get("listener").is_none());
    }

    #[test]
    fn group_text_without_a_valid_sender_and_outgoing_text_are_skipped() {
        let long = format!("{}:\nding", "a".repeat(513));
        for text in [
            "ding",
            ":\nding",
            "other-room@chatroom:\nding",
            "bad\tsender:\nding",
            long.as_str(),
        ] {
            assert_eq!(
                incoming_text(add_message("fixture-room@chatroom", "fixture-self", text)),
                None,
                "{text:?}"
            );
        }
        assert_eq!(
            incoming_text(add_message("fixture-self", "fixture-room@chatroom", "dong")),
            None
        );
        let mut image = add_message("fixture-peer", "fixture-self", "<msg/>");
        image.message_type = 3;
        assert_eq!(incoming_text(image), None);
    }

    #[test]
    fn closing_without_a_session_resets_state() {
        let mut session = Session {
            received_cursor: vec![1, 2, 3],
            ..Default::default()
        };
        assert_eq!(run(&mut session, Operation::Close).unwrap(), Value::Null);
        assert!(session.received_cursor.is_empty());
        assert!(run(&mut session, Operation::LookupSelf).is_err());
        assert!(run(&mut session, Operation::Logout).is_err());
    }

    #[test]
    fn unhandled_sync_commands_are_skipped() {
        let messages = text_messages(
            vec![wire::SyncCommand {
                id: 9999,
                payload: vec![42],
            }],
            "fixture-self",
        );
        assert!(messages.is_empty());
    }
}
