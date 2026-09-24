import Foundation
import EventKit
import Darwin

private let readerVersion = "1.0.0"
private let protocolVersion = 1
private let expectedBundleIdentifier = "io.github.sylviachenxy.sylvia-agent-skills.weekly-review-eventkit-reader"
private let internalInvocationToken = "weekly-review-eventkit-swift-v1-87B451D2"
private let maximumInputBytes = 1_048_576
private let maximumOutputBytes = 4_194_304
private let maximumWindowDays = 62.0
private let maximumIdentifierUTF8Bytes = 4_096
private let maximumTitleUTF8Bytes = 4_096

private struct ReaderFailure: Error {
    let code: String
    let message: String
    let details: [String: Any]

    init(_ code: String, _ message: String, details: [String: Any] = [:]) {
        self.code = code
        self.message = message
        self.details = details
    }
}

private enum Entity: String {
    case event
    case reminder

    var eventKitType: EKEntityType {
        switch self {
        case .event: return .event
        case .reminder: return .reminder
        }
    }

    static func parse(_ value: String) throws -> Entity {
        guard let entity = Entity(rawValue: value) else {
            throw ReaderFailure("validation_error", "entity must be 'event' or 'reminder'.")
        }
        return entity
    }
}

private enum ReminderSelection: String {
    case completedInWindow = "completed_in_window"
    case incompleteDueInWindow = "incomplete_due_in_window"

    static func parse(_ value: String) throws -> ReminderSelection {
        guard let selection = ReminderSelection(rawValue: value) else {
            throw ReaderFailure(
                "validation_error",
                "selection must be 'completed_in_window' or 'incomplete_due_in_window'."
            )
        }
        return selection
    }
}

private func jsonData(_ object: Any, pretty: Bool = false) throws -> Data {
    var options: JSONSerialization.WritingOptions = [.sortedKeys, .withoutEscapingSlashes]
    if pretty { options.insert(.prettyPrinted) }
    return try JSONSerialization.data(withJSONObject: object, options: options)
}

private func outputFitsProtocolLimit(_ data: Data) -> Bool {
    data.count < maximumOutputBytes
}

@discardableResult
private func emit(_ object: [String: Any]) -> Bool {
    do {
        let data = try jsonData(object, pretty: true)
        // Reserve one byte for the trailing newline written below.
        guard outputFitsProtocolLimit(data) else {
            let fallback = "{\"ok\":false,\"reader_version\":\"1.0.0\",\"protocol_version\":1,\"eventkit_data_mutated\":false,\"error\":{\"code\":\"output_limit_exceeded\",\"message\":\"Reader output exceeds the 4 MiB protocol limit.\"}}\n"
            FileHandle.standardOutput.write(Data(fallback.utf8))
            return false
        }
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([0x0A]))
        return true
    } catch {
        let fallback = "{\"ok\":false,\"reader_version\":\"1.0.0\",\"protocol_version\":1,\"eventkit_data_mutated\":false,\"error\":{\"code\":\"serialization_error\",\"message\":\"Unable to serialize reader output.\"}}\n"
        FileHandle.standardOutput.write(Data(fallback.utf8))
        return false
    }
}

private func emitSuccess(_ fields: [String: Any] = [:]) {
    var output = fields
    output["ok"] = true
    output["reader_version"] = readerVersion
    output["protocol_version"] = protocolVersion
    output["eventkit_data_mutated"] = false
    if !emit(output) { Darwin.exit(2) }
}

private func emitFailure(_ failure: ReaderFailure) -> Never {
    var errorObject: [String: Any] = [
        "code": failure.code,
        "message": failure.message
    ]
    if !failure.details.isEmpty { errorObject["details"] = failure.details }
    emit([
        "ok": false,
        "reader_version": readerVersion,
        "protocol_version": protocolVersion,
        "eventkit_data_mutated": false,
        "error": errorObject
    ])
    Darwin.exit(2)
}

private func eventKitFailure(_ code: String, _ operation: String, _ error: Error?) -> ReaderFailure {
    var details: [String: Any] = ["operation": operation]
    if let native = error as NSError? {
        details["domain"] = native.domain
        details["native_code"] = native.code
    }
    return ReaderFailure(code, "EventKit could not complete \(operation).", details: details)
}

private func readInput(required: Bool = true) throws -> [String: Any] {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    guard data.count <= maximumInputBytes else {
        throw ReaderFailure("invalid_json", "stdin exceeds the 1 MiB input limit.")
    }
    if data.isEmpty || String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == true {
        if required { throw ReaderFailure("invalid_json", "A JSON object is required on stdin.") }
        return [:]
    }
    do {
        let value = try JSONSerialization.jsonObject(with: data)
        guard let object = value as? [String: Any] else {
            throw ReaderFailure("invalid_json", "stdin must contain one JSON object.")
        }
        return object
    } catch let failure as ReaderFailure {
        throw failure
    } catch {
        throw ReaderFailure("invalid_json", "stdin is not valid JSON.")
    }
}

private func ensureAllowedKeys(_ object: [String: Any], _ allowed: Set<String>, context: String) throws {
    let unknown = Set(object.keys).subtracting(allowed).sorted()
    if !unknown.isEmpty {
        throw ReaderFailure("validation_error", "Unknown key(s) in \(context).", details: ["keys": unknown])
    }
}

private func requiredString(_ object: [String: Any], _ key: String, context: String = "request") throws -> String {
    guard let value = object[key] as? String, !value.isEmpty else {
        throw ReaderFailure("validation_error", "\(context).\(key) must be a non-empty string.")
    }
    return value
}

private func optionalString(_ object: [String: Any], _ key: String, context: String = "request") throws -> String? {
    guard let raw = object[key] else { return nil }
    if raw is NSNull { return nil }
    guard let value = raw as? String, !value.isEmpty else {
        throw ReaderFailure("validation_error", "\(context).\(key) must be a non-empty string or null.")
    }
    return value
}

private func requiredObject(_ object: [String: Any], _ key: String, context: String = "request") throws -> [String: Any] {
    guard let value = object[key] as? [String: Any] else {
        throw ReaderFailure("validation_error", "\(context).\(key) must be a JSON object.")
    }
    return value
}

private func requiredBool(_ object: [String: Any], _ key: String, context: String = "request") throws -> Bool {
    guard let value = object[key] as? Bool else {
        throw ReaderFailure("validation_error", "\(context).\(key) must be a boolean.")
    }
    return value
}

private func requiredInt(_ object: [String: Any], _ key: String, context: String = "request") throws -> Int {
    guard let number = object[key] as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else {
        throw ReaderFailure("validation_error", "\(context).\(key) must be an integer.")
    }
    let value = number.intValue
    guard NSNumber(value: value) == number else {
        throw ReaderFailure("validation_error", "\(context).\(key) must be an integer.")
    }
    return value
}

private func requiredInt(
    _ object: [String: Any],
    _ key: String,
    range: ClosedRange<Int>,
    context: String = "request"
) throws -> Int {
    let value = try requiredInt(object, key, context: context)
    guard range.contains(value) else {
        throw ReaderFailure(
            "validation_error",
            "\(context).\(key) is outside the supported range.",
            details: ["minimum": range.lowerBound, "maximum": range.upperBound]
        )
    }
    return value
}

private func optionalInt(
    _ object: [String: Any],
    _ key: String,
    default defaultValue: Int,
    range: ClosedRange<Int>,
    context: String = "request"
) throws -> Int {
    guard object[key] != nil else { return defaultValue }
    return try requiredInt(object, key, range: range, context: context)
}

private func stringArray(_ object: [String: Any], _ key: String, context: String = "request") throws -> [String] {
    guard let raw = object[key] as? [Any] else {
        throw ReaderFailure("validation_error", "\(context).\(key) must be an array of strings.")
    }
    let values = try raw.map { item -> String in
        guard let value = item as? String, !value.isEmpty else {
            throw ReaderFailure("validation_error", "\(context).\(key) must contain only non-empty strings.")
        }
        return value
    }
    guard Set(values).count == values.count else {
        throw ReaderFailure("validation_error", "\(context).\(key) must not contain duplicates.")
    }
    return values
}

private func validateOpaqueIdentifiers(_ values: [String], field: String) throws {
    for value in values {
        guard value.utf8.count <= maximumIdentifierUTF8Bytes, !value.contains("\0") else {
            throw ReaderFailure(
                "validation_error",
                "\(field) contains an identifier that exceeds the supported size or contains NUL.",
                details: ["maximum_utf8_bytes": maximumIdentifierUTF8Bytes]
            )
        }
    }
}

private func boundedResultString(
    _ value: String,
    field: String,
    maximumUTF8Bytes: Int,
    allowEmpty: Bool = true
) throws -> String {
    guard (allowEmpty || !value.isEmpty), !value.contains("\0"), value.utf8.count <= maximumUTF8Bytes else {
        throw ReaderFailure(
            "result_field_too_large",
            "EventKit returned an invalid or oversized string field; the result was not emitted.",
            details: ["field": field, "maximum_utf8_bytes": maximumUTF8Bytes]
        )
    }
    return value
}

private let utcFormatter: ISO8601DateFormatter = {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    formatter.timeZone = TimeZone(secondsFromGMT: 0)
    return formatter
}()

private let utcFormatterNoFraction: ISO8601DateFormatter = {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime]
    formatter.timeZone = TimeZone(secondsFromGMT: 0)
    return formatter
}()

private func isoString(_ date: Date?) -> Any {
    guard let date else { return NSNull() }
    return utcFormatter.string(from: date)
}

private func nullableString(_ value: String?) -> Any {
    guard let value else { return NSNull() }
    return value
}

private func parseDateTime(_ value: String, field: String) throws -> Date {
    guard value.range(of: "(?:[zZ]|[+-][0-9]{2}:[0-9]{2})$", options: .regularExpression) != nil else {
        throw ReaderFailure("validation_error", "\(field) must be RFC 3339 with an explicit UTC offset.")
    }
    if let date = utcFormatter.date(from: value) ?? utcFormatterNoFraction.date(from: value) { return date }
    throw ReaderFailure("validation_error", "\(field) is not a valid RFC 3339 timestamp.")
}

private struct DateWindow {
    let start: Date
    let end: Date

    init(input: [String: Any], context: String = "request.window") throws {
        try ensureAllowedKeys(input, ["start_at", "end_at"], context: context)
        start = try parseDateTime(try requiredString(input, "start_at", context: context), field: "\(context).start_at")
        end = try parseDateTime(try requiredString(input, "end_at", context: context), field: "\(context).end_at")
        guard start < end else {
            throw ReaderFailure("validation_error", "\(context).start_at must be earlier than end_at.")
        }
        guard end.timeIntervalSince(start) <= maximumWindowDays * 86_400 else {
            throw ReaderFailure(
                "validation_error",
                "\(context) exceeds the supported window.",
                details: ["maximum_days": maximumWindowDays]
            )
        }
    }

    var jsonObject: [String: Any] {
        ["start_at": isoString(start), "end_at": isoString(end)]
    }
}

private func authorizationName(_ status: EKAuthorizationStatus) -> String {
    switch status.rawValue {
    case 0: return "not_determined"
    case 1: return "restricted"
    case 2: return "denied"
    case 3: return "full_access"
    case 4: return "write_only"
    default: return "unknown_\(status.rawValue)"
    }
}

private func sourceTypeName(_ type: EKSourceType) -> String {
    switch type {
    case .local: return "local"
    case .exchange: return "exchange"
    case .calDAV: return "caldav"
    case .mobileMe: return "mobileme"
    case .subscribed: return "subscribed"
    case .birthdays: return "birthdays"
    @unknown default: return "unknown_\(type.rawValue)"
    }
}

private func availabilityName(_ value: EKEventAvailability) -> String {
    switch value {
    case .busy: return "busy"
    case .free: return "free"
    case .tentative: return "tentative"
    case .unavailable: return "unavailable"
    case .notSupported: return "not_supported"
    @unknown default: return "unknown_\(value.rawValue)"
    }
}

private func eventStatusName(_ value: EKEventStatus) -> String {
    switch value {
    case .none: return "none"
    case .confirmed: return "confirmed"
    case .tentative: return "tentative"
    case .canceled: return "canceled"
    @unknown default: return "unknown_\(value.rawValue)"
    }
}

private func entityMaskNames(_ mask: EKEntityMask) -> [String] {
    var values: [String] = []
    if mask.contains(.event) { values.append("event") }
    if mask.contains(.reminder) { values.append("reminder") }
    return values
}

private final class StoreContext {
    let store = EKEventStore()

    func requireFullAccess(_ entity: Entity) throws {
        let status = EKEventStore.authorizationStatus(for: entity.eventKitType)
        guard status.rawValue == 3 else {
            let code: String
            switch status.rawValue {
            case 0: code = "permission_not_determined"
            case 1: code = "permission_restricted"
            case 2: code = "permission_denied"
            case 4: code = "permission_write_only"
            default: code = "permission_unavailable"
            }
            throw ReaderFailure(
                code,
                "Full \(entity.rawValue) access is required for this read-only query.",
                details: ["status": authorizationName(status)]
            )
        }
    }

    func requireCalendars(_ identifiers: [String], entity: Entity) throws -> [EKCalendar] {
        try identifiers.map { identifier in
            guard let calendar = store.calendar(withIdentifier: identifier) else {
                throw ReaderFailure("container_missing", "A selected container is unavailable.", details: ["container_id": identifier])
            }
            let mask: EKEntityMask = entity == .event ? .event : .reminder
            guard calendar.allowedEntityTypes.contains(mask) else {
                throw ReaderFailure(
                    "container_type_mismatch",
                    "A selected container does not support the requested entity.",
                    details: ["container_id": identifier, "entity": entity.rawValue]
                )
            }
            return calendar
        }
    }
}

private func requireSelectedIDs(_ input: [String: Any], key: String) throws -> [String] {
    let values = try stringArray(input, key)
    guard !values.isEmpty, values.count <= 50 else {
        throw ReaderFailure(
            "validation_error",
            "\(key) must contain 1–50 explicitly selected container IDs."
        )
    }
    try validateOpaqueIdentifiers(values, field: "request.\(key)")
    return values
}

private func sourceJSON(_ source: EKSource, entity: Entity) throws -> [String: Any] {
    [
        "source_id": try boundedResultString(source.sourceIdentifier, field: "source.source_id", maximumUTF8Bytes: maximumIdentifierUTF8Bytes, allowEmpty: false),
        "title": try boundedResultString(source.title, field: "source.title", maximumUTF8Bytes: maximumTitleUTF8Bytes),
        "source_type": sourceTypeName(source.sourceType),
        "container_count": source.calendars(for: entity.eventKitType).count
    ]
}

private func containerJSON(_ calendar: EKCalendar) throws -> [String: Any] {
    [
        "container_id": try boundedResultString(calendar.calendarIdentifier, field: "container.container_id", maximumUTF8Bytes: maximumIdentifierUTF8Bytes, allowEmpty: false),
        "title": try boundedResultString(calendar.title, field: "container.title", maximumUTF8Bytes: maximumTitleUTF8Bytes),
        "source_id": try boundedResultString(calendar.source.sourceIdentifier, field: "container.source_id", maximumUTF8Bytes: maximumIdentifierUTF8Bytes, allowEmpty: false),
        "source_title": try boundedResultString(calendar.source.title, field: "container.source_title", maximumUTF8Bytes: maximumTitleUTF8Bytes),
        "source_type": sourceTypeName(calendar.source.sourceType),
        "allowed_entities": entityMaskNames(calendar.allowedEntityTypes)
    ]
}

private func rawCalendarItemID(_ item: EKCalendarItem) -> String {
    if let event = item as? EKEvent {
        return event.eventIdentifier ?? event.calendarItemIdentifier
    }
    return item.calendarItemIdentifier
}

private func calendarItemID(_ item: EKCalendarItem) throws -> String {
    try boundedResultString(
        rawCalendarItemID(item),
        field: "item.item_id",
        maximumUTF8Bytes: maximumIdentifierUTF8Bytes,
        allowEmpty: false
    )
}

private func allDayDateString(_ date: Date) -> String {
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = TimeZone.current
    let components = calendar.dateComponents([.year, .month, .day], from: date)
    return String(
        format: "%04d-%02d-%02d",
        components.year ?? -1,
        components.month ?? -1,
        components.day ?? -1
    )
}

private func timingJSON(_ event: EKEvent) -> [String: Any] {
    if event.isAllDay {
        return [
            "kind": "all_day",
            "start_date": allDayDateString(event.startDate),
            "end_date_exclusive": allDayDateString(event.endDate)
        ]
    }
    return [
        "kind": "timed",
        "start_at": isoString(event.startDate),
        "end_at": isoString(event.endDate),
        "timezone": nullableString(event.timeZone?.identifier)
    ]
}

private func eventJSON(_ event: EKEvent, detail: String) throws -> [String: Any] {
    var object: [String: Any] = [
        "item_id": try calendarItemID(event),
        "calendar_id": try boundedResultString(event.calendar.calendarIdentifier, field: "event.calendar_id", maximumUTF8Bytes: maximumIdentifierUTF8Bytes, allowEmpty: false),
        "time": timingJSON(event),
        "availability": availabilityName(event.availability),
        "status": eventStatusName(event.status),
        "recurring": event.hasRecurrenceRules || !(event.recurrenceRules?.isEmpty ?? true) || event.isDetached
    ]
    if detail == "summary" {
        object["title"] = try boundedResultString(event.title ?? "", field: "event.title", maximumUTF8Bytes: maximumTitleUTF8Bytes)
        object["created_at"] = isoString(event.creationDate)
        object["last_modified_at"] = isoString(event.lastModifiedDate)
    }
    return object
}

private func componentsJSON(_ components: DateComponents?, fallbackTimeZone: TimeZone?) -> Any {
    guard let components else { return NSNull() }
    var object: [String: Any] = [:]
    if let year = components.year { object["year"] = year }
    if let month = components.month { object["month"] = month }
    if let day = components.day { object["day"] = day }
    if let hour = components.hour { object["hour"] = hour }
    if let minute = components.minute { object["minute"] = minute }
    if let second = components.second { object["second"] = second }
    if let timeZone = components.timeZone ?? fallbackTimeZone { object["timezone"] = timeZone.identifier }
    object["kind"] = components.hour == nil && components.minute == nil && components.second == nil ? "date" : "date_time"
    return object
}

private func reminderJSON(_ reminder: EKReminder) throws -> [String: Any] {
    [
        "item_id": try calendarItemID(reminder),
        "list_id": try boundedResultString(reminder.calendar.calendarIdentifier, field: "reminder.list_id", maximumUTF8Bytes: maximumIdentifierUTF8Bytes, allowEmpty: false),
        "title": try boundedResultString(reminder.title ?? "", field: "reminder.title", maximumUTF8Bytes: maximumTitleUTF8Bytes),
        "due": componentsJSON(reminder.dueDateComponents, fallbackTimeZone: reminder.timeZone),
        "completed": reminder.isCompleted,
        "completion_at": isoString(reminder.completionDate),
        "priority": reminder.priority,
        "recurring": reminder.hasRecurrenceRules || !(reminder.recurrenceRules?.isEmpty ?? true),
        "created_at": isoString(reminder.creationDate),
        "last_modified_at": isoString(reminder.lastModifiedDate)
    ]
}

private func fetchReminders(
    store: EKEventStore,
    predicate: NSPredicate,
    timeoutSeconds: Int
) throws -> [EKReminder] {
    let semaphore = DispatchSemaphore(value: 0)
    let lock = NSLock()
    var terminal = false
    var result: [EKReminder]?
    let token = store.fetchReminders(matching: predicate) { reminders in
        lock.lock()
        guard !terminal else {
            lock.unlock()
            return
        }
        terminal = true
        result = reminders
        lock.unlock()
        semaphore.signal()
    }
    if semaphore.wait(timeout: .now() + .seconds(timeoutSeconds)) == .timedOut {
        lock.lock()
        if !terminal {
            terminal = true
            lock.unlock()
            store.cancelFetchRequest(token)
            throw ReaderFailure(
                "timeout",
                "EventKit reminder fetch timed out and was cancelled.",
                details: ["timeout_seconds": timeoutSeconds]
            )
        }
        lock.unlock()
    }
    lock.lock()
    let reminders = result
    lock.unlock()
    guard let reminders else {
        throw ReaderFailure("eventkit_error", "EventKit returned no reminder result.")
    }
    return reminders
}

private func commandCapabilities() {
    emitSuccess([
        "command": "capabilities",
        "commands": [
            "capabilities", "doctor", "self-test", "authorize", "sources list",
            "containers list", "events list", "reminders list"
        ],
        "platform": ["os": "macOS", "minimum_supported": "14.0"],
        "access": [
            "eventkit_data_mode": "read_only",
            "full_access_required_for_reads": true,
            "authorization_requires_confirmed_true": true,
            "query_requires_explicit_container_ids": true,
            "maximum_container_ids": 50,
            "maximum_window_days": maximumWindowDays,
            "maximum_result_limit": 500,
            "maximum_input_bytes": maximumInputBytes,
            "maximum_output_bytes": maximumOutputBytes,
            "maximum_identifier_utf8_bytes": maximumIdentifierUTF8Bytes,
            "maximum_title_utf8_bytes": maximumTitleUTF8Bytes
        ],
        "privacy": [
            "never_returns": ["notes", "attendees", "organizer", "url", "alarms", "recurrence_rules"],
            "event_details": ["busy", "summary"],
            "busy_omits_titles": true,
            "summary_returns_titles": true
        ],
        "eventkit_data_accessed": false
    ])
}

private func commandDoctor() {
    let bundleID = Bundle.main.bundleIdentifier
    let plist = Bundle.main.infoDictionary ?? [:]
    let signatureKind: String = (plist["WeeklyReviewSignatureKind"] as? String) ?? "unknown"
    emitSuccess([
        "command": "doctor",
        "bundle": [
            "actual_id": nullableString(bundleID),
            "expected_id": expectedBundleIdentifier,
            "identity_matches": bundleID == expectedBundleIdentifier,
            "signature_kind": signatureKind,
            "tcc_identity_stable_across_rebuild": false,
            "ad_hoc_rebuild_may_require_reauthorization": true,
            "sandboxed": false,
            "bundle_path": Bundle.main.bundlePath,
            "calendar_usage_description_present": !(plist["NSCalendarsFullAccessUsageDescription"] as? String ?? "").isEmpty,
            "reminders_usage_description_present": !(plist["NSRemindersFullAccessUsageDescription"] as? String ?? "").isEmpty
        ],
        "permissions": [
            "events": authorizationName(EKEventStore.authorizationStatus(for: .event)),
            "reminders": authorizationName(EKEventStore.authorizationStatus(for: .reminder))
        ],
        "platform": [
            "os": "macOS",
            "version": ProcessInfo.processInfo.operatingSystemVersionString,
            "minimum_supported": "14.0"
        ],
        "eventkit_data_accessed": false
    ])
}

private func commandSelfTest() throws {
    let window = try DateWindow(input: [
        "start_at": "2026-08-31T00:00:00+08:00",
        "end_at": "2026-09-07T00:00:00+08:00"
    ], context: "self_test.window")
    guard window.end.timeIntervalSince(window.start) == 7 * 86_400 else {
        throw ReaderFailure("self_test_failed", "RFC 3339 window parsing failed.")
    }
    guard try ReminderSelection.parse("completed_in_window") == .completedInWindow,
          try ReminderSelection.parse("incomplete_due_in_window") == .incompleteDueInWindow else {
        throw ReaderFailure("self_test_failed", "Reminder selection parsing failed.")
    }
    let privacyForbidden = Set(["notes", "attendees", "organizer", "url", "alarms", "recurrence_rules"])
    let eventBusyFields = Set(["item_id", "calendar_id", "time", "availability", "status", "recurring"])
    let eventSummaryFields = eventBusyFields.union(["title", "created_at", "last_modified_at"])
    let reminderFields = Set([
        "item_id", "list_id", "title", "due", "completed", "completion_at", "priority",
        "recurring", "created_at", "last_modified_at"
    ])
    guard eventBusyFields.isDisjoint(with: privacyForbidden),
          eventSummaryFields.isDisjoint(with: privacyForbidden),
          reminderFields.isDisjoint(with: privacyForbidden) else {
        throw ReaderFailure("self_test_failed", "The output field allowlists violate the privacy contract.")
    }
    guard outputFitsProtocolLimit(Data(count: maximumOutputBytes - 1)),
          !outputFitsProtocolLimit(Data(count: maximumOutputBytes)) else {
        throw ReaderFailure("self_test_failed", "The output-size boundary check failed.")
    }
    var oversizedResultRejected = false
    do {
        _ = try boundedResultString(
            String(repeating: "x", count: maximumTitleUTF8Bytes + 1),
            field: "self_test.title",
            maximumUTF8Bytes: maximumTitleUTF8Bytes
        )
    } catch let failure as ReaderFailure where failure.code == "result_field_too_large" {
        oversizedResultRejected = true
    }
    guard oversizedResultRejected else {
        throw ReaderFailure("self_test_failed", "Oversized EventKit string rejection failed.")
    }
    var oversizedInputIDRejected = false
    do {
        try validateOpaqueIdentifiers(
            [String(repeating: "x", count: maximumIdentifierUTF8Bytes + 1)],
            field: "self_test.ids"
        )
    } catch let failure as ReaderFailure where failure.code == "validation_error" {
        oversizedInputIDRejected = true
    }
    guard oversizedInputIDRejected else {
        throw ReaderFailure("self_test_failed", "Oversized input identifier rejection failed.")
    }
    emitSuccess([
        "command": "self-test",
        "tests": [
            ["name": "rfc3339_window", "passed": true],
            ["name": "reminder_selection", "passed": true],
            ["name": "privacy_field_allowlists", "passed": true],
            ["name": "output_size_boundary", "passed": true],
            ["name": "oversized_result_string_rejected", "passed": true],
            ["name": "oversized_input_identifier_rejected", "passed": true],
            ["name": "read_only_command_surface", "passed": true]
        ],
        "eventkit_data_accessed": false,
        "production_state_accessed": false
    ])
}

private func commandAuthorize(_ input: [String: Any]) throws {
    try ensureAllowedKeys(input, ["entity", "confirmed", "timeout_seconds"], context: "request")
    let entity = try Entity.parse(try requiredString(input, "entity"))
    guard try requiredBool(input, "confirmed") else {
        throw ReaderFailure(
            "confirmation_required",
            "Requesting macOS Calendar or Reminders permission requires explicit user confirmation."
        )
    }
    let timeoutSeconds = try optionalInt(input, "timeout_seconds", default: 120, range: 30...300)
    let initial = EKEventStore.authorizationStatus(for: entity.eventKitType)
    if initial.rawValue == 3 {
        emitSuccess([
            "command": "authorize",
            "entity": entity.rawValue,
            "status": "full_access",
            "prompted": false,
            "permission_state_changed": false,
            "eventkit_data_accessed": false
        ])
        return
    }
    switch initial.rawValue {
    case 1:
        throw ReaderFailure("permission_restricted", "macOS restricts this permission; the reader did not prompt again.")
    case 2:
        throw ReaderFailure("permission_denied", "This permission was previously denied; the reader did not prompt again.")
    case 4:
        throw ReaderFailure("permission_write_only", "Write-only Calendar access is insufficient; the reader did not prompt again.")
    case 0:
        break
    default:
        throw ReaderFailure("permission_unavailable", "The EventKit authorization state is unsupported.")
    }
    guard #available(macOS 14.0, *) else {
        throw ReaderFailure("unsupported_platform", "Full EventKit access requires macOS 14 or later.")
    }

    let store = EKEventStore()
    let semaphore = DispatchSemaphore(value: 0)
    let lock = NSLock()
    var granted = false
    var requestError: Error?
    let completion: EKEventStoreRequestAccessCompletionHandler = { result, error in
        lock.lock()
        granted = result
        requestError = error
        lock.unlock()
        semaphore.signal()
    }
    switch entity {
    case .event: store.requestFullAccessToEvents(completion: completion)
    case .reminder: store.requestFullAccessToReminders(completion: completion)
    }
    if semaphore.wait(timeout: .now() + .seconds(timeoutSeconds)) == .timedOut {
        throw ReaderFailure(
            "timeout",
            "The macOS permission request did not finish before the timeout.",
            details: ["timeout_seconds": timeoutSeconds]
        )
    }
    lock.lock()
    let finalGranted = granted
    let finalError = requestError
    lock.unlock()
    if let finalError { throw eventKitFailure("permission_request_failed", "permission request", finalError) }
    let final = EKEventStore.authorizationStatus(for: entity.eventKitType)
    guard finalGranted, final.rawValue == 3 else {
        throw ReaderFailure("permission_denied", "Full EventKit access was not granted.", details: ["status": authorizationName(final)])
    }
    emitSuccess([
        "command": "authorize",
        "entity": entity.rawValue,
        "status": authorizationName(final),
        "prompted": true,
        "permission_state_changed": initial.rawValue != final.rawValue,
        "eventkit_data_accessed": false
    ])
}

private func commandSourcesList(_ input: [String: Any]) throws {
    try ensureAllowedKeys(input, ["entity"], context: "request")
    let entity = try Entity.parse(try requiredString(input, "entity"))
    let context = StoreContext()
    try context.requireFullAccess(entity)
    context.store.refreshSourcesIfNecessary()
    let sources = try context.store.sources
        .filter { !$0.calendars(for: entity.eventKitType).isEmpty }
        .map { try sourceJSON($0, entity: entity) }
        .sorted {
            let left = ($0["title"] as? String ?? "") + "\u{0}" + ($0["source_id"] as? String ?? "")
            let right = ($1["title"] as? String ?? "") + "\u{0}" + ($1["source_id"] as? String ?? "")
            return left.localizedCaseInsensitiveCompare(right) == .orderedAscending
        }
    emitSuccess([
        "command": "sources list",
        "entity": entity.rawValue,
        "event_store_id": try boundedResultString(context.store.eventStoreIdentifier, field: "event_store_id", maximumUTF8Bytes: maximumIdentifierUTF8Bytes, allowEmpty: false),
        "sources": sources,
        "eventkit_data_accessed": true
    ])
}

private func commandContainersList(_ input: [String: Any]) throws {
    try ensureAllowedKeys(input, ["entity", "source_id"], context: "request")
    let entity = try Entity.parse(try requiredString(input, "entity"))
    let sourceID = try optionalString(input, "source_id")
    if let sourceID { try validateOpaqueIdentifiers([sourceID], field: "request.source_id") }
    let context = StoreContext()
    try context.requireFullAccess(entity)
    let containers = try context.store.calendars(for: entity.eventKitType)
        .filter { sourceID == nil || $0.source.sourceIdentifier == sourceID }
        .map { try containerJSON($0) }
        .sorted {
            let left = ($0["source_title"] as? String ?? "") + "\u{0}" + ($0["title"] as? String ?? "")
            let right = ($1["source_title"] as? String ?? "") + "\u{0}" + ($1["title"] as? String ?? "")
            return left.localizedCaseInsensitiveCompare(right) == .orderedAscending
        }
    emitSuccess([
        "command": "containers list",
        "entity": entity.rawValue,
        "source_id": nullableString(sourceID),
        "event_store_id": try boundedResultString(context.store.eventStoreIdentifier, field: "event_store_id", maximumUTF8Bytes: maximumIdentifierUTF8Bytes, allowEmpty: false),
        "containers": containers,
        "eventkit_data_accessed": true
    ])
}

private func commandEventsList(_ input: [String: Any]) throws {
    try ensureAllowedKeys(input, ["calendar_ids", "window", "detail", "limit"], context: "request")
    let calendarIDs = try requireSelectedIDs(input, key: "calendar_ids")
    let window = try DateWindow(input: try requiredObject(input, "window"))
    let detail = try requiredString(input, "detail")
    guard detail == "busy" || detail == "summary" else {
        throw ReaderFailure("validation_error", "detail must be 'busy' or 'summary'.")
    }
    let limit = try requiredInt(input, "limit", range: 1...500)
    let context = StoreContext()
    try context.requireFullAccess(.event)
    let calendars = try context.requireCalendars(calendarIDs, entity: .event)
    let predicate = context.store.predicateForEvents(withStart: window.start, end: window.end, calendars: calendars)
    var events = context.store.events(matching: predicate)
    if detail == "busy" {
        events = events.filter { $0.status != .canceled && $0.availability != .free }
    }
    events.sort {
        if $0.startDate != $1.startDate { return $0.startDate < $1.startDate }
        let left = $0.calendar.calendarIdentifier + "\u{0}" + rawCalendarItemID($0)
        let right = $1.calendar.calendarIdentifier + "\u{0}" + rawCalendarItemID($1)
        return left < right
    }
    let truncated = events.count > limit
    if truncated { events = Array(events.prefix(limit)) }
    emitSuccess([
        "command": "events list",
        "detail": detail,
        "calendar_ids": calendarIDs,
        "window": window.jsonObject,
        "limit": limit,
        "event_store_id": try boundedResultString(context.store.eventStoreIdentifier, field: "event_store_id", maximumUTF8Bytes: maximumIdentifierUTF8Bytes, allowEmpty: false),
        "items": try events.map { try eventJSON($0, detail: detail) },
        "truncated": truncated,
        "eventkit_data_accessed": true
    ])
}

private func commandRemindersList(_ input: [String: Any]) throws {
    try ensureAllowedKeys(input, ["list_ids", "window", "selection", "limit", "timeout_seconds"], context: "request")
    let listIDs = try requireSelectedIDs(input, key: "list_ids")
    let window = try DateWindow(input: try requiredObject(input, "window"))
    let selection = try ReminderSelection.parse(try requiredString(input, "selection"))
    let limit = try requiredInt(input, "limit", range: 1...500)
    let timeoutSeconds = try optionalInt(input, "timeout_seconds", default: 20, range: 5...60)
    let context = StoreContext()
    try context.requireFullAccess(.reminder)
    let lists = try context.requireCalendars(listIDs, entity: .reminder)
    let predicate: NSPredicate
    switch selection {
    case .completedInWindow:
        predicate = context.store.predicateForCompletedReminders(
            withCompletionDateStarting: window.start,
            ending: window.end,
            calendars: lists
        )
    case .incompleteDueInWindow:
        predicate = context.store.predicateForIncompleteReminders(
            withDueDateStarting: window.start,
            ending: window.end,
            calendars: lists
        )
    }
    var reminders = try fetchReminders(store: context.store, predicate: predicate, timeoutSeconds: timeoutSeconds)
    reminders = reminders.filter { reminder in
        switch selection {
        case .completedInWindow:
            guard reminder.isCompleted, let completionDate = reminder.completionDate else { return false }
            return completionDate >= window.start && completionDate < window.end
        case .incompleteDueInWindow:
            guard !reminder.isCompleted,
                  let dueDate = dateFromComponents(reminder.dueDateComponents, fallbackTimeZone: reminder.timeZone) else {
                return false
            }
            return dueDate >= window.start && dueDate < window.end
        }
    }
    reminders.sort {
        let leftDate = selection == .completedInWindow ? $0.completionDate : dateFromComponents($0.dueDateComponents, fallbackTimeZone: $0.timeZone)
        let rightDate = selection == .completedInWindow ? $1.completionDate : dateFromComponents($1.dueDateComponents, fallbackTimeZone: $1.timeZone)
        if leftDate != rightDate { return (leftDate ?? .distantFuture) < (rightDate ?? .distantFuture) }
        return $0.calendar.calendarIdentifier + "\u{0}" + rawCalendarItemID($0) < $1.calendar.calendarIdentifier + "\u{0}" + rawCalendarItemID($1)
    }
    let truncated = reminders.count > limit
    if truncated { reminders = Array(reminders.prefix(limit)) }
    emitSuccess([
        "command": "reminders list",
        "selection": selection.rawValue,
        "list_ids": listIDs,
        "window": window.jsonObject,
        "limit": limit,
        "event_store_id": try boundedResultString(context.store.eventStoreIdentifier, field: "event_store_id", maximumUTF8Bytes: maximumIdentifierUTF8Bytes, allowEmpty: false),
        "items": try reminders.map { try reminderJSON($0) },
        "truncated": truncated,
        "eventkit_data_accessed": true
    ])
}

private func dateFromComponents(_ components: DateComponents?, fallbackTimeZone: TimeZone?) -> Date? {
    guard let components else { return nil }
    var calendar = components.calendar ?? Calendar(identifier: .gregorian)
    calendar.timeZone = components.timeZone ?? fallbackTimeZone ?? TimeZone.current
    return calendar.date(from: components)
}

private func requireStableBundleIdentity() throws {
    guard Bundle.main.bundleIdentifier == expectedBundleIdentifier else {
        throw ReaderFailure(
            "bundle_identity_mismatch",
            "Run this reader through scripts/apple-eventkit-reader.sh so macOS can associate permissions with its app identity.",
            details: [
                "expected_bundle_id": expectedBundleIdentifier,
                "actual_bundle_id": nullableString(Bundle.main.bundleIdentifier)
            ]
        )
    }
}

private func requireInternalInvocation() throws {
    guard ProcessInfo.processInfo.environment["WEEKLY_REVIEW_EVENTKIT_SWIFT_TOKEN"] == internalInvocationToken else {
        throw ReaderFailure(
            "internal_entrypoint_required",
            "Use scripts/apple-eventkit-reader.sh; the native EventKit app is an internal transport."
        )
    }
}

@main
private struct WeeklyReviewEventKitReader {
    static func main() {
        do {
            try requireInternalInvocation()
            let command = Array(CommandLine.arguments.dropFirst()).joined(separator: " ")
            switch command {
            case "capabilities":
                commandCapabilities()
            case "doctor":
                commandDoctor()
            case "self-test":
                try commandSelfTest()
            case "authorize":
                try requireStableBundleIdentity()
                try commandAuthorize(try readInput())
            case "sources list":
                try requireStableBundleIdentity()
                try commandSourcesList(try readInput())
            case "containers list":
                try requireStableBundleIdentity()
                try commandContainersList(try readInput())
            case "events list":
                try requireStableBundleIdentity()
                try commandEventsList(try readInput())
            case "reminders list":
                try requireStableBundleIdentity()
                try commandRemindersList(try readInput())
            default:
                throw ReaderFailure(
                    "unknown_command",
                    "Unknown command.",
                    details: [
                        "received": command,
                        "supported": [
                            "capabilities", "doctor", "self-test", "authorize", "sources list",
                            "containers list", "events list", "reminders list"
                        ]
                    ]
                )
            }
        } catch let failure as ReaderFailure {
            emitFailure(failure)
        } catch {
            emitFailure(
                ReaderFailure(
                    "internal_error",
                    "The reader encountered an unexpected internal error.",
                    details: ["type": String(describing: type(of: error))]
                )
            )
        }
    }
}
