use std::ffi::c_char;

#[repr(C)]
pub struct AuraBookingRequest {
    room_id: i64,
    start_unix: i64,
    end_unix: i64,
    user_id: i64,
    attendee_count: i32,
}

const OK: i32 = 0;
const INVALID_ROOM: i32 = 1;
const INVALID_USER: i32 = 2;
const INVALID_INTERVAL: i32 = 3;
const INVALID_CAPACITY: i32 = 4;
const INVALID_TRANSITION: i32 = 5;

#[no_mangle]
pub extern "C" fn aura_validate_booking(request: *const AuraBookingRequest, room_capacity: i32) -> i32 {
    if request.is_null() || room_capacity <= 0 { return INVALID_ROOM; }
    let request = unsafe { &*request };
    if request.room_id <= 0 { return INVALID_ROOM; }
    if request.user_id <= 0 { return INVALID_USER; }
    if request.end_unix <= request.start_unix { return INVALID_INTERVAL; }
    if request.attendee_count <= 0 || request.attendee_count > room_capacity { return INVALID_CAPACITY; }
    OK
}

#[no_mangle]
pub extern "C" fn aura_validate_workflow_transition(kind: *const c_char, from: *const c_char, to: *const c_char) -> i32 {
    if kind.is_null() || from.is_null() || to.is_null() {
        return INVALID_TRANSITION;
    }
    let kind = unsafe { std::ffi::CStr::from_ptr(kind) }.to_bytes();
    let from = unsafe { std::ffi::CStr::from_ptr(from) }.to_bytes();
    let to = unsafe { std::ffi::CStr::from_ptr(to) }.to_bytes();
    let valid = match kind {
        b"task" => matches!((from, to),
            (b"open", b"in_progress") | (b"open", b"done") | (b"open", b"cancelled") |
            (b"in_progress", b"open") | (b"in_progress", b"done") | (b"in_progress", b"cancelled") |
            (b"done", b"open") | (b"done", b"in_progress") |
            (b"cancelled", b"open")),
        b"ticket" => matches!((from, to),
            (b"open", b"in_progress") | (b"open", b"closed") |
            (b"in_progress", b"resolved") | (b"in_progress", b"closed") | (b"in_progress", b"open") |
            (b"resolved", b"closed") | (b"resolved", b"in_progress") |
            (b"closed", b"open")),
        _ => false,
    };
    if valid { OK } else { INVALID_TRANSITION }
}

#[no_mangle]
pub extern "C" fn aura_rule_message(code: i32) -> *const c_char {
    match code {
        OK => b"OK\0".as_ptr() as *const c_char,
        INVALID_ROOM => b"Ungueltiger Raum\0".as_ptr() as *const c_char,
        INVALID_USER => b"Ungueltiger Benutzer\0".as_ptr() as *const c_char,
        INVALID_INTERVAL => b"Endzeit muss nach der Startzeit liegen\0".as_ptr() as *const c_char,
        INVALID_CAPACITY => b"Kapazitaet des Raums ueberschritten\0".as_ptr() as *const c_char,
        INVALID_TRANSITION => b"Ungueltiger Workflow-Statuswechsel\0".as_ptr() as *const c_char,
        _ => b"Unbekannter Regelcode\0".as_ptr() as *const c_char,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn accepts_a_valid_booking() {
        let request = AuraBookingRequest { room_id: 1, start_unix: 10, end_unix: 20, user_id: 1, attendee_count: 2 };
        assert_eq!(aura_validate_booking(&request, 2), OK);
    }
    #[test]
    fn rejects_over_capacity() {
        let request = AuraBookingRequest { room_id: 1, start_unix: 10, end_unix: 20, user_id: 1, attendee_count: 3 };
        assert_eq!(aura_validate_booking(&request, 2), INVALID_CAPACITY);
    }
    #[test]
    fn accepts_only_defined_workflow_transitions() {
        let task = std::ffi::CString::new("task").unwrap();
        let open = std::ffi::CString::new("open").unwrap();
        let done = std::ffi::CString::new("done").unwrap();
        let closed = std::ffi::CString::new("closed").unwrap();
        assert_eq!(aura_validate_workflow_transition(task.as_ptr(), open.as_ptr(), done.as_ptr()), OK);
        assert_eq!(aura_validate_workflow_transition(task.as_ptr(), done.as_ptr(), closed.as_ptr()), INVALID_TRANSITION);
    }
}
