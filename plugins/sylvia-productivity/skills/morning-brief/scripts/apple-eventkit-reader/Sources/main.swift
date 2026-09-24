import Foundation
import EventKit
import Darwin

// This target has no EventKit mutation command or native data-writing API.
private let readerVersion = "1.0.0"
private let bundleID = "io.github.sylviachenxy.sylvia-agent-skills.morning-brief-eventkit-reader"
private let invocationToken = "morning-brief-eventkit-read-v1-B89E3F52"
private let maximumInputBytes = 1_048_576
private let maximumOutputBytes = 4_194_304
private let maximumWindowDays = 100.0
private let commands = ["capabilities", "doctor", "self-test", "setup authorize", "setup containers list", "events list", "reminders list"]

private struct ReaderFailure: Error {
    let code: String
    let message: String
    init(_ code: String, _ message: String) { self.code = code; self.message = message }
}

private func emit(_ payload: [String: Any], failure: Bool = false) {
    var value = payload
    value["ok"] = !failure
    value["reader_version"] = readerVersion
    value["protocol_version"] = 1
    value["eventkit_data_mutated"] = false
    guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .withoutEscapingSlashes]), data.count < maximumOutputBytes else {
        FileHandle.standardOutput.write(Data("{\"ok\":false,\"reader_version\":\"1.0.0\",\"protocol_version\":1,\"eventkit_data_mutated\":false,\"error\":{\"code\":\"output_limit_exceeded\",\"message\":\"The result could not be emitted within the protocol limit.\"}}\n".utf8))
        Darwin.exit(2)
    }
    FileHandle.standardOutput.write(data + Data([10]))
    if failure { Darwin.exit(2) }
}

private func requireKeys(_ input: [String: Any], _ allowed: Set<String>) throws {
    guard Set(input.keys).isSubset(of: allowed) else { throw ReaderFailure("validation_error", "The request contains unsupported fields.") }
}

private func string(_ input: [String: Any], _ key: String) throws -> String {
    guard let value = input[key] as? String, !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
          value.utf8.count <= 4096, !value.contains("\0") else {
        throw ReaderFailure("validation_error", "A required string field is missing or invalid.")
    }
    return value
}

private func boolean(_ input: [String: Any], _ key: String, default fallback: Bool? = nil) throws -> Bool {
    if input[key] == nil, let fallback { return fallback }
    guard let value = input[key] as? NSNumber, CFGetTypeID(value) == CFBooleanGetTypeID() else {
        throw ReaderFailure("validation_error", "A required boolean field is missing or invalid.")
    }
    return value.boolValue
}

private func integer(_ input: [String: Any], _ key: String, default fallback: Int? = nil, range: ClosedRange<Int>) throws -> Int {
    if input[key] == nil, let fallback { return fallback }
    guard let value = input[key] as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID(),
          value == NSNumber(value: value.intValue), range.contains(value.intValue) else {
        throw ReaderFailure("validation_error", "A required integer field is missing or outside its supported range.")
    }
    return value.intValue
}

private func selectedIDs(_ input: [String: Any], _ key: String) throws -> [String] {
    guard let values = input[key] as? [String], !values.isEmpty, values.count <= 50, Set(values).count == values.count else {
        throw ReaderFailure("validation_error", "Queries require 1–50 distinct, explicitly selected container IDs.")
    }
    for value in values { _ = try string(["id": value], "id") }
    return values
}

private func bounded(_ value: String?, id: Bool = false) throws -> String {
    let value = value ?? ""
    guard value.utf8.count <= 4096, !value.contains("\0"), !id || !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
        throw ReaderFailure("result_field_invalid", "A native result contains an invalid or oversized field; no partial value was emitted.")
    }
    return value
}

private let formatter: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]; return f
}()
private let plainFormatter = ISO8601DateFormatter()
private func iso(_ value: Date?) -> Any { value.map { formatter.string(from: $0) } ?? NSNull() }
private func nullable(_ value: String?) -> Any { value.map { $0 as Any } ?? NSNull() }
private func timestamp(_ value: String) throws -> Date {
    guard value.range(of: "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$", options: .regularExpression) != nil,
          let result = formatter.date(from: value) ?? plainFormatter.date(from: value) else {
        throw ReaderFailure("validation_error", "Time windows require valid RFC 3339 timestamps with explicit offsets.")
    }
    return result
}

private func queryZone(_ input: [String: Any]) throws -> TimeZone {
    let name = try string(input, "timezone")
    guard name == "UTC" || TimeZone.knownTimeZoneIdentifiers.contains(name), let zone = TimeZone(identifier: name) else {
        throw ReaderFailure("validation_error", "timezone must identify a supported IANA timezone.")
    }
    return zone
}

private struct Window {
    let start: Date
    let end: Date
    init(_ input: [String: Any]) throws {
        try requireKeys(input, ["start_at", "end_at"])
        start = try timestamp(string(input, "start_at")); end = try timestamp(string(input, "end_at"))
        guard start < end, end.timeIntervalSince(start) <= maximumWindowDays * 86_400 else {
            throw ReaderFailure("validation_error", "A source window must have positive elapsed duration of at most 100 days.")
        }
    }
    var json: [String: Any] { ["start_at": iso(start), "end_at": iso(end)] }
}

private func requestWindow(_ input: [String: Any]) throws -> Window {
    guard let object = input["window"] as? [String: Any] else { throw ReaderFailure("validation_error", "window must be an object.") }
    return try Window(object)
}

private func gregorian(_ zone: TimeZone) -> Calendar {
    var calendar = Calendar(identifier: .gregorian); calendar.timeZone = zone; return calendar
}

private func dateString(_ date: Date, zone: TimeZone) -> String {
    let parts = gregorian(zone).dateComponents([.year, .month, .day], from: date)
    return String(format: "%04d-%02d-%02d", parts.year ?? 0, parts.month ?? 0, parts.day ?? 0)
}

private struct Due {
    let components: DateComponents?
    let nativeZone: TimeZone?
    let effectiveZone: TimeZone
    let day: String?
    let instant: Date?
    let dateOnly: Bool

    init(_ components: DateComponents?, nativeZone: TimeZone?, queryZone: TimeZone) throws {
        self.components = components
        self.nativeZone = components?.timeZone ?? nativeZone
        effectiveZone = components?.timeZone ?? nativeZone ?? queryZone
        guard let c = components else { day = nil; instant = nil; dateOnly = false; return }
        guard let year = c.year, let month = c.month, let dayNumber = c.day, year >= 1, year <= 9999 else {
            throw ReaderFailure("invalid_due_components", "A reminder has unsupported due-date components; coverage is unavailable.")
        }
        dateOnly = c.hour == nil && c.minute == nil && c.second == nil
        var calendar = c.calendar ?? Calendar(identifier: .gregorian)
        guard calendar.identifier == .gregorian || calendar.identifier == .iso8601 else {
            throw ReaderFailure("unsupported_due_calendar", "A reminder uses an unsupported due-date calendar.")
        }
        calendar.timeZone = effectiveZone
        var normalized = DateComponents()
        normalized.year = year; normalized.month = month; normalized.day = dayNumber
        normalized.hour = c.hour ?? 0; normalized.minute = c.minute ?? 0; normalized.second = c.second ?? 0
        guard let parsed = calendar.date(from: normalized) else { throw ReaderFailure("invalid_due_components", "A reminder due date could not be interpreted.") }
        let check = calendar.dateComponents([.year, .month, .day, .hour, .minute, .second], from: parsed)
        guard check.year == year, check.month == month, check.day == dayNumber,
              check.hour == normalized.hour, check.minute == normalized.minute, check.second == normalized.second else {
            throw ReaderFailure("invalid_due_components", "A reminder due date is invalid or falls in a nonexistent local time.")
        }
        if !dateOnly {
            let first = calendar.date(bySettingHour: c.hour ?? 0, minute: c.minute ?? 0, second: c.second ?? 0, of: parsed, matchingPolicy: .strict, repeatedTimePolicy: .first)
            let last = calendar.date(bySettingHour: c.hour ?? 0, minute: c.minute ?? 0, second: c.second ?? 0, of: parsed, matchingPolicy: .strict, repeatedTimePolicy: .last)
            guard first == last else {
                throw ReaderFailure("ambiguous_due_time", "A reminder local due time is repeated by a timezone transition; its exact instant cannot be inferred safely.")
            }
        }
        day = dateOnly ? String(format: "%04d-%02d-%02d", year, month, dayNumber) : nil
        instant = dateOnly ? nil : parsed
    }

    func included(_ window: Window, zone: TimeZone, includeUndated: Bool) -> Bool {
        if let instant { return instant >= window.start && instant < window.end }
        if let day {
            let firstDay = dateString(window.start, zone: zone)
            let lastDay = dateString(window.end, zone: zone)
            // A date-only deadline denotes its whole civil day, never midnight overdue.
            let includesLastDay = window.end > gregorian(zone).startOfDay(for: window.end)
            return day >= firstDay && (day < lastDay || (includesLastDay && day == lastDay))
        }
        return includeUndated
    }

    func sortDate(zone: TimeZone) -> Date {
        if let instant { return instant }
        guard let c = components, dateOnly else { return .distantFuture }
        var civil = DateComponents(); civil.year = c.year; civil.month = c.month; civil.day = c.day
        let calendar = gregorian(zone)
        guard let start = calendar.date(from: civil), let next = calendar.date(byAdding: .day, value: 1, to: start) else { return .distantFuture }
        // Use the end of the civil day, not 00:00; timed deadlines that day precede it.
        return next.addingTimeInterval(-0.001)
    }

    var json: Any {
        guard let c = components else { return NSNull() }
        var output: [String: Any] = ["kind": dateOnly ? "date" : "date_time", "timezone": nullable(nativeZone?.identifier), "effective_timezone": effectiveZone.identifier, "timezone_inferred_from_request": nativeZone == nil]
        for (key, value) in [("year", c.year), ("month", c.month), ("day", c.day), ("hour", c.hour), ("minute", c.minute), ("second", c.second)] { if let value { output[key] = value } }
        return output
    }
}

private func managedProjection(_ notes: String?, entity: String, enabled: Bool) -> [String: Any] {
    func result(_ status: String, _ managed: Any = NSNull()) -> [String: Any] { ["managed_status": status, "managed": managed] }
    guard enabled else { return result("not_requested") }
    let text = notes ?? ""
    guard text.utf8.count <= 262_144 else { return result("oversized") }
    let lines = text.components(separatedBy: .newlines).map { $0.trimmingCharacters(in: .whitespaces) }
    let markers = lines.filter { $0.contains("[goal-planner:") }
    let ends = lines.filter { $0.contains("[/goal-planner]") }
    guard !markers.isEmpty || !ends.isEmpty else { return result("absent") }
    guard markers.allSatisfy({ $0 == "[goal-planner:v2]" }) else { return result("unsupported") }
    guard markers.count == 1, ends == ["[/goal-planner]"], let first = lines.firstIndex(of: "[goal-planner:v2]"), let last = lines.firstIndex(of: "[/goal-planner]"), first < last else { return result("malformed") }
    var values: [String: String] = [:]
    let allowed: Set<String> = ["goal_id", "action_id", "projection_id", "role", "goal_path", "obsidian_url"]
    for line in lines[(first + 1)..<last] where !line.isEmpty {
        guard let split = line.firstIndex(of: "=") else { return result("malformed") }
        let key = String(line[..<split]), value = String(line[line.index(after: split)...])
        guard allowed.contains(key), values[key] == nil, !value.isEmpty, value.utf8.count <= 4096, !value.contains("\0") else { return result("malformed") }
        values[key] = value
    }
    guard let goal = values["goal_id"], goal.range(of: "^G-[0-9]{4}-[0-9]{3}$", options: .regularExpression) != nil,
          let projection = values["projection_id"], projection.range(of: "^" + goal + (entity == "event" ? "-E" : "-R") + "[0-9]{3}$", options: .regularExpression) != nil,
          let role = values["role"], (entity == "event" ? ["work-block", "check-in", "deadline"] : ["action", "check-in"]).contains(role),
          values["goal_path"] == "Goals/\(goal)/\(goal).md", let link = values["obsidian_url"], URL(string: link)?.scheme?.lowercased() == "obsidian" else { return result("malformed") }
    if let action = values["action_id"], action.range(of: "^" + goal + "-A[0-9]{3}$", options: .regularExpression) == nil { return result("malformed") }
    return result("valid", ["schema_version": 2, "goal_id": goal, "action_id": nullable(values["action_id"]), "projection_id": projection])
}

private enum Entity: String {
    case event, reminder
    var type: EKEntityType { self == .event ? .event : .reminder }
    static func parse(_ input: [String: Any]) throws -> Entity {
        guard let value = Entity(rawValue: try string(input, "entity")) else { throw ReaderFailure("validation_error", "entity must be event or reminder.") }
        return value
    }
}

private func authorizationName(_ raw: Int) -> String {
    [0: "not_determined", 1: "restricted", 2: "denied", 3: "full_access", 4: "write_only"][raw] ?? "unknown"
}

private func authorizedStore(_ entity: Entity) throws -> EKEventStore {
    let status = EKEventStore.authorizationStatus(for: entity.type).rawValue
    guard status == 3 else { throw ReaderFailure("permission_" + authorizationName(status), "Full access is unavailable. This query did not request permission.") }
    return EKEventStore()
}

private func calendars(_ ids: [String], _ entity: Entity, _ store: EKEventStore) throws -> [EKCalendar] {
    try ids.map { id in
        guard let container = store.calendar(withIdentifier: id), container.allowedEntityTypes.contains(entity == .event ? .event : .reminder) else {
            throw ReaderFailure("container_unavailable", "A selected container is missing or has the wrong entity type.")
        }
        return container
    }
}

private func nativeID(_ item: EKCalendarItem) -> String { (item as? EKEvent)?.eventIdentifier ?? item.calendarItemIdentifier }
private func status(_ value: EKEventStatus) -> String {
    switch value { case .none: return "none"; case .confirmed: return "confirmed"; case .tentative: return "tentative"; case .canceled: return "canceled"; @unknown default: return "unknown" }
}
private func availability(_ value: EKEventAvailability) -> String {
    switch value { case .busy: return "busy"; case .free: return "free"; case .tentative: return "tentative"; case .unavailable: return "unavailable"; case .notSupported: return "not_supported"; @unknown default: return "unknown" }
}

private func eventTiming(start: Date, end: Date, allDay: Bool, nativeZone: TimeZone?, zone: TimeZone, originalOccurrence: Date?) -> [String: Any] {
    var result: [String: Any] = ["start_at": iso(start), "end_at": iso(end), "all_day": allDay, "timezone": nullable(nativeZone?.identifier), "occurrence_start_at": iso(start), "original_occurrence_at": iso(originalOccurrence)]
    if allDay {
        // EventKit SDK: floating/all-day startDate and occurrenceDate use the process default zone.
        // Source commands explicitly set that process-local default to the requested zone before fetching.
        result["start_date"] = dateString(start, zone: zone)
        result["end_date_exclusive"] = dateString(end, zone: zone)
        result["date_timezone"] = zone.identifier
        result["date_timezone_inferred_from_request"] = true
    }
    return result
}

private func eventJSON(_ event: EKEvent, zone: TimeZone, goalLinks: Bool) throws -> [String: Any] {
    var output: [String: Any] = [
        "item_id": try bounded(nativeID(event), id: true), "calendar_id": try bounded(event.calendar.calendarIdentifier, id: true),
        "title": try bounded(event.title),
        "status": status(event.status), "availability": availability(event.availability),
        "recurring": event.hasRecurrenceRules || event.isDetached, "detached": event.isDetached,
        "created_at": iso(event.creationDate), "last_modified_at": iso(event.lastModifiedDate)
    ]
    output.merge(eventTiming(start: event.startDate, end: event.endDate, allDay: event.isAllDay, nativeZone: event.timeZone, zone: zone, originalOccurrence: event.occurrenceDate)) { _, new in new }
    output.merge(managedProjection(goalLinks ? event.notes : nil, entity: "event", enabled: goalLinks)) { _, new in new }
    return output
}

private func reminderJSON(_ reminder: EKReminder, due: Due, goalLinks: Bool) throws -> [String: Any] {
    var output: [String: Any] = [
        "item_id": try bounded(nativeID(reminder), id: true), "list_id": try bounded(reminder.calendar.calendarIdentifier, id: true),
        "title": try bounded(reminder.title), "due_date": nullable(due.day), "due_at": iso(due.instant), "due": due.json,
        "completed": reminder.isCompleted, "completion_at": iso(reminder.completionDate), "priority": reminder.priority,
        "recurring": reminder.hasRecurrenceRules, "current_instance_only": true,
        "created_at": iso(reminder.creationDate), "last_modified_at": iso(reminder.lastModifiedDate)
    ]
    output.merge(managedProjection(goalLinks ? reminder.notes : nil, entity: "reminder", enabled: goalLinks)) { _, new in new }
    return output
}

private func sourceEnvelope(_ command: String, window: Window, zone: TimeZone, store: EKEventStore, scope: [String: Any], items: [[String: Any]], total: Int, limit: Int, started: Date) throws -> [String: Any] {
    let truncated = total > limit
    return [
        "command": command, "event_store_id": try bounded(store.eventStoreIdentifier, id: true),
        "coverage": truncated ? "partial" : "complete", "as_of": iso(started), "collected_through": iso(Date()),
        "query_window": window.json, "timezone": zone.identifier, "scope": scope,
        "result_count": items.count, "matched_count": total, "limit": limit, "truncated": truncated,
        "truncated_reason": truncated ? "result_limit" as Any : NSNull(), "error": NSNull(),
        "items": items, "eventkit_data_accessed": true
    ]
}

private func commandEvents(_ input: [String: Any]) throws {
    try requireKeys(input, ["calendar_ids", "window", "timezone", "limit", "include_goal_links"])
    let ids = try selectedIDs(input, "calendar_ids"), window = try requestWindow(input), zone = try queryZone(input)
    let limit = try integer(input, "limit", range: 1...500), links = try boolean(input, "include_goal_links", default: false)
    NSTimeZone.default = zone // Process-local only; does not change the Mac or user calendar settings.
    let started = Date(), store = try authorizedStore(.event), selected = try calendars(ids, .event, store)
    let predicate = store.predicateForEvents(withStart: window.start, end: window.end, calendars: selected)
    // Explicit overlap filter makes the half-open endpoint contract independent of native predicate inclusivity.
    var matches = store.events(matching: predicate).filter { event in
        selected.contains(where: { $0.calendarIdentifier == event.calendar.calendarIdentifier }) &&
        event.startDate < window.end && (event.endDate > window.start || (event.startDate == event.endDate && event.startDate >= window.start))
    }
    matches.sort {
        if $0.startDate != $1.startDate { return $0.startDate < $1.startDate }
        return $0.calendar.calendarIdentifier + "\0" + nativeID($0) < $1.calendar.calendarIdentifier + "\0" + nativeID($1)
    }
    let scope: [String: Any] = ["calendar_ids": ids, "backend_query_window": window.json, "candidate_mode": "events_overlapping_window", "include_goal_links": links, "notes_exported": false, "cancelled_and_free_included": true]
    emit(try sourceEnvelope("events list", window: window, zone: zone, store: store, scope: scope, items: matches.prefix(limit).map { try eventJSON($0, zone: zone, goalLinks: links) }, total: matches.count, limit: limit, started: started))
}

private func fetch(_ store: EKEventStore, _ predicate: NSPredicate, timeout: Int) throws -> [EKReminder] {
    let semaphore = DispatchSemaphore(value: 0), lock = NSLock()
    var result: [EKReminder]?, finished = false
    let token = store.fetchReminders(matching: predicate) { values in
        lock.lock(); defer { lock.unlock() }
        guard !finished else { return }; result = values; finished = true; semaphore.signal()
    }
    if semaphore.wait(timeout: .now() + .seconds(timeout)) == .timedOut {
        lock.lock(); finished = true; lock.unlock(); store.cancelFetchRequest(token)
        throw ReaderFailure("operation_timeout", "The read-only reminder query timed out and was cancelled.")
    }
    lock.lock(); let values = result; lock.unlock()
    guard let values else { throw ReaderFailure("native_read_error", "EventKit returned no reminder result; this is not a zero-item result.") }
    return values
}

private func commandReminders(_ input: [String: Any]) throws {
    try requireKeys(input, ["list_ids", "window", "timezone", "limit", "include_undated", "include_goal_links", "timeout_seconds"])
    let ids = try selectedIDs(input, "list_ids"), window = try requestWindow(input), zone = try queryZone(input)
    let limit = try integer(input, "limit", range: 1...500), timeout = try integer(input, "timeout_seconds", default: 20, range: 5...60)
    let includeUndated = try boolean(input, "include_undated"), links = try boolean(input, "include_goal_links", default: false)
    NSTimeZone.default = zone
    let started = Date(), store = try authorizedStore(.reminder), selected = try calendars(ids, .reminder, store)
    let calendar = gregorian(zone)
    // A disclosed 48-hour guard on civil-day boundaries handles floating dates and timezone offsets.
    // Results outside the requested civil/timed window are discarded before sorting and truncation.
    let backendStart = calendar.startOfDay(for: window.start).addingTimeInterval(-48 * 3600)
    let backendEnd = calendar.startOfDay(for: window.end).addingTimeInterval(72 * 3600)
    let predicate = store.predicateForIncompleteReminders(withDueDateStarting: includeUndated ? nil : backendStart, ending: includeUndated ? nil : backendEnd, calendars: selected)
    let candidates = try fetch(store, predicate, timeout: timeout)
    var matches: [(EKReminder, Due)] = []
    for reminder in candidates where !reminder.isCompleted && ids.contains(reminder.calendar.calendarIdentifier) {
        let due = try Due(reminder.dueDateComponents, nativeZone: reminder.timeZone, queryZone: zone)
        if due.included(window, zone: zone, includeUndated: includeUndated) { matches.append((reminder, due)) }
    }
    matches.sort {
        let left = $0.1.sortDate(zone: zone), right = $1.1.sortDate(zone: zone)
        if left != right { return left < right }
        return $0.0.calendar.calendarIdentifier + "\0" + nativeID($0.0) < $1.0.calendar.calendarIdentifier + "\0" + nativeID($1.0)
    }
    let backendWindow: Any = includeUndated ? NSNull() : ["start_at": iso(backendStart), "end_at": iso(backendEnd)]
    let scope: [String: Any] = ["list_ids": ids, "backend_query_window": backendWindow, "candidate_mode": includeUndated ? "all_incomplete_in_selected_lists" : "incomplete_due_with_civil_day_timezone_guard", "candidate_count": candidates.count, "include_undated": includeUndated, "include_goal_links": links, "completed_included": false, "notes_exported": false, "sort": "due_instant_or_end_of_civil_due_day_then_id; undated_last"]
    emit(try sourceEnvelope("reminders list", window: window, zone: zone, store: store, scope: scope, items: matches.prefix(limit).map { try reminderJSON($0.0, due: $0.1, goalLinks: links) }, total: matches.count, limit: limit, started: started))
}

private func commandAuthorize(_ input: [String: Any]) throws {
    try requireKeys(input, ["entity", "confirmed", "timeout_seconds"])
    let entity = try Entity.parse(input), timeout = try integer(input, "timeout_seconds", default: 120, range: 30...180)
    guard try boolean(input, "confirmed") else { throw ReaderFailure("confirmation_required", "Permission setup requires explicit user confirmation.") }
    let initial = EKEventStore.authorizationStatus(for: entity.type).rawValue
    if initial == 3 { emit(["command": "setup authorize", "entity": entity.rawValue, "status": "full_access", "prompted": false, "permission_state_changed": false, "eventkit_data_accessed": false]); return }
    guard initial == 0 else { throw ReaderFailure("permission_" + authorizationName(initial), "The permission is unavailable; setup did not repeat a denied or restricted request.") }
    let store = EKEventStore(), semaphore = DispatchSemaphore(value: 0), lock = NSLock()
    var granted = false, failed = false
    let completion: EKEventStoreRequestAccessCompletionHandler = { value, error in lock.lock(); granted = value; failed = error != nil; lock.unlock(); semaphore.signal() }
    if entity == .event { store.requestFullAccessToEvents(completion: completion) } else { store.requestFullAccessToReminders(completion: completion) }
    guard semaphore.wait(timeout: .now() + .seconds(timeout)) != .timedOut else { throw ReaderFailure("operation_timeout", "Permission setup did not finish before its timeout.") }
    lock.lock(); let accepted = granted && !failed; lock.unlock()
    guard accepted, EKEventStore.authorizationStatus(for: entity.type).rawValue == 3 else { throw ReaderFailure("permission_not_granted", "Full EventKit permission was not granted.") }
    emit(["command": "setup authorize", "entity": entity.rawValue, "status": "full_access", "prompted": true, "permission_state_changed": true, "eventkit_data_accessed": false])
}

private func commandContainers(_ input: [String: Any]) throws {
    try requireKeys(input, ["entity", "confirmed"])
    let entity = try Entity.parse(input)
    guard try boolean(input, "confirmed") else { throw ReaderFailure("confirmation_required", "Listing container metadata requires explicit setup confirmation.") }
    let store = try authorizedStore(entity)
    let containers = try store.calendars(for: entity.type).map { calendar -> [String: Any] in
        ["container_id": try bounded(calendar.calendarIdentifier, id: true), "title": try bounded(calendar.title), "source_id": try bounded(calendar.source.sourceIdentifier, id: true), "source_title": try bounded(calendar.source.title)]
    }.sorted { ($0["container_id"] as? String ?? "") < ($1["container_id"] as? String ?? "") }
    guard containers.count <= 1000 else { throw ReaderFailure("container_limit_exceeded", "Too many containers were returned; metadata was not emitted.") }
    emit(["command": "setup containers list", "entity": entity.rawValue, "event_store_id": try bounded(store.eventStoreIdentifier, id: true), "scope": ["metadata_only": true, "all_containers_for_entity": true, "item_contents_read": false], "containers": containers, "eventkit_data_accessed": true])
}

private func selfTest() throws {
    var checks: [String] = []
    func check(_ value: Bool, _ name: String) throws { guard value else { throw ReaderFailure("self_test_failed", "An offline semantic invariant failed.") }; checks.append(name) }
    let zone = TimeZone(identifier: "Asia/Shanghai")!
    let window = try Window(["start_at": "2026-09-02T06:00:00+08:00", "end_at": "2026-09-02T21:00:00+08:00"])
    try check(window.start < window.end, "future_window_has_no_now_cutoff")
    let boundStart = try timestamp("2026-06-04T00:00:00+08:00")
    let bound = try Window(["start_at": iso(boundStart), "end_at": iso(boundStart.addingTimeInterval(100 * 86_400))])
    try check(bound.end.timeIntervalSince(bound.start) == 100 * 86_400, "exact_100_elapsed_day_source_window")
    var oversizedWindowRejected = false
    do { _ = try Window(["start_at": iso(boundStart), "end_at": iso(boundStart.addingTimeInterval(100 * 86_400 + 1))]) } catch { oversizedWindowRejected = true }
    try check(oversizedWindowRejected, "source_window_over_100_elapsed_days_rejected")
    let overdue90 = try Window(["start_at": "2026-06-04T00:00:00+08:00", "end_at": "2026-09-09T23:59:00+08:00"])
    try check(overdue90.end.timeIntervalSince(overdue90.start) < 99 * 86_400, "90_day_overdue_plus_latest_config_forecast_supported")
    var today = DateComponents(); today.year = 2026; today.month = 9; today.day = 2
    let dueToday = try Due(today, nativeZone: nil, queryZone: zone)
    try check(dueToday.included(window, zone: zone, includeUndated: false) && dueToday.instant == nil && dueToday.day == "2026-09-02", "date_only_today_not_midnight_overdue")
    var timed = today; timed.hour = 8; timed.minute = 30
    let dueTimed = try Due(timed, nativeZone: zone, queryZone: zone)
    try check(dueTimed.instant == (try timestamp("2026-09-02T08:30:00+08:00")) && dueTimed.day == nil, "timed_due_keeps_native_timezone")
    var earlier = today; earlier.day = 1
    let dueEarlier = try Due(earlier, nativeZone: nil, queryZone: zone)
    let undated = try Due(nil, nativeZone: nil, queryZone: zone)
    let sorted = [dueToday, undated, dueTimed, dueEarlier].sorted { $0.sortDate(zone: zone) < $1.sortDate(zone: zone) }
    try check(sorted.prefix(2).map { $0.day ?? "timed" } == ["2026-09-01", "timed"], "actual_date_sort_precedes_truncation")
    try check(!dueEarlier.included(window, zone: zone, includeUndated: true) && !undated.included(window, zone: zone, includeUndated: false) && undated.included(window, zone: zone, includeUndated: true), "bounded_overdue_and_explicit_undated")
    let midnight = try Window(["start_at": "2026-09-01T21:00:00+08:00", "end_at": "2026-09-02T00:00:00+08:00"])
    try check(!dueToday.included(midnight, zone: zone, includeUndated: false), "date_only_exclusive_end_day")
    let marker = "Private body must never escape\n[goal-planner:v2]\ngoal_id=G-2026-001\nprojection_id=G-2026-001-R001\naction_id=G-2026-001-A001\nrole=action\ngoal_path=Goals/G-2026-001/G-2026-001.md\nobsidian_url=obsidian://open?vault=PRIVATE\n[/goal-planner]"
    let safe = managedProjection(marker, entity: "reminder", enabled: true)
    try check(safe["managed_status"] as? String == "valid", "managed_v2_association")
    let data = try JSONSerialization.data(withJSONObject: safe)
    let serialized = String(decoding: data, as: UTF8.self)
    try check(!serialized.contains("PRIVATE") && !serialized.contains("Private body") && !serialized.contains("obsidian_url") && !serialized.contains("goal_path"), "managed_projection_excludes_body_and_url")
    try check(managedProjection(marker + "\n" + marker, entity: "reminder", enabled: true)["managed_status"] as? String == "malformed", "duplicate_marker_rejected")
    try check(managedProjection(marker.replacingOccurrences(of: "[goal-planner:v2]", with: "[goal-planner:v1]"), entity: "reminder", enabled: true)["managed_status"] as? String == "unsupported", "unsupported_marker_rejected")
    try check(managedProjection(marker, entity: "event", enabled: true)["managed_status"] as? String == "malformed", "marker_entity_conflict_rejected")
    try check(managedProjection(marker, entity: "reminder", enabled: false)["managed_status"] as? String == "not_requested", "goal_metadata_opt_in")
    var badIDRejected = false
    do { _ = try selectedIDs(["ids": [" \n"]], "ids") } catch { badIDRejected = true }
    try check(badIDRejected, "blank_container_id_rejected")
    var booleanNumberRejected = false
    do { _ = try boolean(["flag": 1], "flag") } catch { booleanNumberRejected = true }
    try check(booleanNumberRejected, "boolean_number_rejected")
    try check(status(.canceled) == "canceled" && status(.tentative) == "tentative" && availability(.free) == "free", "native_status_preserved")
    let allDay = eventTiming(start: try timestamp("2026-09-02T00:00:00+08:00"), end: try timestamp("2026-09-04T00:00:00+08:00"), allDay: true, nativeZone: nil, zone: zone, originalOccurrence: nil)
    try check(allDay["start_date"] as? String == "2026-09-02" && allDay["end_date_exclusive"] as? String == "2026-09-04", "all_day_end_exclusive")
    let occurrence = eventTiming(start: try timestamp("2026-09-02T11:00:00+08:00"), end: try timestamp("2026-09-02T12:00:00+08:00"), allDay: false, nativeZone: zone, zone: zone, originalOccurrence: try timestamp("2026-09-02T10:00:00+08:00"))
    try check(occurrence["occurrence_start_at"] as? String == occurrence["start_at"] as? String && occurrence["occurrence_start_at"] as? String != occurrence["original_occurrence_at"] as? String, "detached_occurrence_actual_and_original_time")
    var ambiguous = DateComponents(); ambiguous.year = 2026; ambiguous.month = 11; ambiguous.day = 1; ambiguous.hour = 1; ambiguous.minute = 30
    var ambiguousRejected = false
    do { _ = try Due(ambiguous, nativeZone: TimeZone(identifier: "America/New_York")!, queryZone: zone) } catch let failure as ReaderFailure { ambiguousRejected = failure.code == "ambiguous_due_time" }
    try check(ambiguousRejected, "ambiguous_dst_due_time_fails_closed")
    emit(["command": "self-test", "tests": checks.map { ["name": $0, "passed": true] as [String: Any] }, "eventkit_data_accessed": false, "native_store_initialized": false, "production_state_accessed": false])
}

@main
private struct MorningBriefEventKitReader {
    static func main() {
        do {
            guard ProcessInfo.processInfo.environment["MORNING_BRIEF_EVENTKIT_INTERNAL"] == invocationToken else { throw ReaderFailure("internal_entrypoint_required", "Use scripts/apple-eventkit-reader.sh; the app executable is an internal transport.") }
            let command = CommandLine.arguments.dropFirst().joined(separator: " ")
            switch command {
            case "capabilities":
                emit(["command": command, "commands": commands, "access": ["mode": "read_only", "maximum_window_days": maximumWindowDays, "maximum_container_ids": 50, "maximum_result_limit": 500, "goal_marker_opt_in": true, "notes_exported": false, "full_urls_exported": false], "eventkit_data_accessed": false])
                return
            case "doctor":
                // No EKEventStore instance, authorizationStatus, or native permission call in doctor.
                emit(["command": command, "bundle_identity_matches": Bundle.main.bundleIdentifier == bundleID, "minimum_macos": "14.0", "permissions": "not_checked_offline", "signature_kind": "adhoc", "rebuild_may_require_reauthorization": true, "eventkit_data_accessed": false, "native_store_initialized": false])
                return
            case "self-test": try selfTest(); return
            default: break
            }
            guard commands.contains(command) else { throw ReaderFailure("unknown_command", "This read-only command is not supported.") }
            guard Bundle.main.bundleIdentifier == bundleID else { throw ReaderFailure("bundle_identity_mismatch", "The reader app bundle identity could not be verified.") }
            let data = FileHandle.standardInput.readDataToEndOfFile()
            guard !data.isEmpty, data.count <= maximumInputBytes, let input = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw ReaderFailure("invalid_json", "Expected one bounded JSON request object.") }
            switch command {
            case "setup authorize": try commandAuthorize(input)
            case "setup containers list": try commandContainers(input)
            case "events list": try commandEvents(input)
            case "reminders list": try commandReminders(input)
            default: throw ReaderFailure("unknown_command", "This read-only command is not supported.")
            }
        } catch let failure as ReaderFailure {
            emit(["error": ["code": failure.code, "message": failure.message]], failure: true)
        } catch {
            // Native exceptions may contain titles, notes or account details. Never echo them.
            emit(["error": ["code": "internal_error", "message": "The read-only operation failed; native error details were suppressed."]], failure: true)
        }
    }
}
