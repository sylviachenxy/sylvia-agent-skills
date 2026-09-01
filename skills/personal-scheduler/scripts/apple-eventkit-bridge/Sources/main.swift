import Foundation
import EventKit
import CryptoKit
import Darwin

private let bridgeVersion = "1.0.0"
private let protocolVersion = 1
private let managedSchemaVersion = 1
private let expectedBundleIdentifier = "io.github.sylviachenxy.sylvia-agent-skills.personal-scheduler-eventkit"
private let internalInvocationToken = "personal-scheduler-swift-v1-4C8E1A73"
private let managedStartMarker = "[personal-scheduler:v1]"
private let managedEndMarker = "[/personal-scheduler]"

private struct BridgeFailure: Error {
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
            throw BridgeFailure("validation_error", "entity must be 'event' or 'reminder'.")
        }
        return entity
    }
}

private func jsonData(_ object: Any, pretty: Bool = false) throws -> Data {
    var options: JSONSerialization.WritingOptions = [.sortedKeys, .withoutEscapingSlashes]
    if pretty { options.insert(.prettyPrinted) }
    return try JSONSerialization.data(withJSONObject: object, options: options)
}

@discardableResult
private func emit(_ object: [String: Any]) -> Bool {
    do {
        let data = try jsonData(object, pretty: true)
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([0x0A]))
        return true
    } catch {
        let fallback = "{\"ok\":false,\"bridge_version\":\"\(bridgeVersion)\",\"protocol_version\":\(protocolVersion),\"managed_metadata_schema_version\":\(managedSchemaVersion),\"error\":{\"code\":\"serialization_error\",\"message\":\"Unable to serialize bridge output.\"}}\n"
        FileHandle.standardOutput.write(Data(fallback.utf8))
        return false
    }
}

private func emitSuccess(_ fields: [String: Any] = [:]) {
    var output = fields
    output["ok"] = true
    output["bridge_version"] = bridgeVersion
    output["protocol_version"] = protocolVersion
    output["managed_metadata_schema_version"] = managedSchemaVersion
    if !emit(output) {
        Darwin.exit(2)
    }
}

private func emitFailure(_ failure: BridgeFailure) -> Never {
    var errorObject: [String: Any] = [
        "code": failure.code,
        "message": failure.message
    ]
    if !failure.details.isEmpty {
        errorObject["details"] = failure.details
    }
    emit([
        "ok": false,
        "bridge_version": bridgeVersion,
        "protocol_version": protocolVersion,
        "managed_metadata_schema_version": managedSchemaVersion,
        "error": errorObject
    ])
    Darwin.exit(2)
}

private func eventKitFailure(_ code: String, _ operation: String, _ error: Error?) -> BridgeFailure {
    var details: [String: Any] = ["operation": operation]
    if let nsError = error as NSError? {
        details["domain"] = nsError.domain
        details["native_code"] = nsError.code
    }
    return BridgeFailure(code, "EventKit could not complete \(operation).", details: details)
}

private func readInput(required: Bool = true) throws -> [String: Any] {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    if data.isEmpty || String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == true {
        if required {
            throw BridgeFailure("invalid_json", "A JSON object is required on stdin.")
        }
        return [:]
    }
    do {
        let value = try JSONSerialization.jsonObject(with: data)
        guard let object = value as? [String: Any] else {
            throw BridgeFailure("invalid_json", "stdin must contain one JSON object.")
        }
        return object
    } catch let failure as BridgeFailure {
        throw failure
    } catch {
        throw BridgeFailure("invalid_json", "stdin is not valid JSON.")
    }
}

private func ensureAllowedKeys(_ object: [String: Any], _ allowed: Set<String>, context: String) throws {
    let unknown = Set(object.keys).subtracting(allowed).sorted()
    if !unknown.isEmpty {
        throw BridgeFailure("validation_error", "Unknown key(s) in \(context).", details: ["keys": unknown])
    }
}

private func requiredString(_ object: [String: Any], _ key: String, context: String = "request") throws -> String {
    guard let value = object[key] as? String, !value.isEmpty else {
        throw BridgeFailure("validation_error", "\(context).\(key) must be a non-empty string.")
    }
    return value
}

private func optionalString(_ object: [String: Any], _ key: String, context: String = "request") throws -> String? {
    guard let raw = object[key] else { return nil }
    if raw is NSNull { return nil }
    guard let value = raw as? String else {
        throw BridgeFailure("validation_error", "\(context).\(key) must be a string or null.")
    }
    return value
}

private func requiredObject(_ object: [String: Any], _ key: String, context: String = "request") throws -> [String: Any] {
    guard let value = object[key] as? [String: Any] else {
        throw BridgeFailure("validation_error", "\(context).\(key) must be a JSON object.")
    }
    return value
}

private func optionalObject(_ object: [String: Any], _ key: String, context: String = "request") throws -> [String: Any]? {
    guard let raw = object[key] else { return nil }
    if raw is NSNull { return nil }
    guard let value = raw as? [String: Any] else {
        throw BridgeFailure("validation_error", "\(context).\(key) must be a JSON object or null.")
    }
    return value
}

private func requiredBool(_ object: [String: Any], _ key: String, context: String = "request") throws -> Bool {
    guard let value = object[key] as? Bool else {
        throw BridgeFailure("validation_error", "\(context).\(key) must be a boolean.")
    }
    return value
}

private func optionalBool(_ object: [String: Any], _ key: String, default defaultValue: Bool, context: String = "request") throws -> Bool {
    guard object[key] != nil else { return defaultValue }
    return try requiredBool(object, key, context: context)
}

private func requiredInt(_ object: [String: Any], _ key: String, context: String = "request") throws -> Int {
    guard let number = object[key] as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else {
        throw BridgeFailure("validation_error", "\(context).\(key) must be an integer.")
    }
    let value = number.intValue
    guard NSNumber(value: value) == number else {
        throw BridgeFailure("validation_error", "\(context).\(key) must be an integer.")
    }
    return value
}

private func optionalInt(_ object: [String: Any], _ key: String, default defaultValue: Int, range: ClosedRange<Int>, context: String = "request") throws -> Int {
    guard object[key] != nil else { return defaultValue }
    let value = try requiredInt(object, key, context: context)
    guard range.contains(value) else {
        throw BridgeFailure("validation_error", "\(context).\(key) is outside the supported range.", details: ["minimum": range.lowerBound, "maximum": range.upperBound])
    }
    return value
}

private func stringArray(_ object: [String: Any], _ key: String, context: String = "request") throws -> [String] {
    guard let raw = object[key] as? [Any] else {
        throw BridgeFailure("validation_error", "\(context).\(key) must be an array of strings.")
    }
    let values = try raw.map { value -> String in
        guard let string = value as? String, !string.isEmpty else {
            throw BridgeFailure("validation_error", "\(context).\(key) must contain only non-empty strings.")
        }
        return string
    }
    guard Set(values).count == values.count else {
        throw BridgeFailure("validation_error", "\(context).\(key) must not contain duplicates.")
    }
    return values
}

private func matches(_ value: String, pattern: String) -> Bool {
    value.range(of: pattern, options: .regularExpression) != nil
}

private func rejectLineBreaks(_ value: String, field: String) throws {
    if value.contains("\n") || value.contains("\r") {
        throw BridgeFailure("validation_error", "\(field) must not contain line breaks.")
    }
}

private func validateScheduleIDShape(_ value: String, field: String = "schedule_id") throws {
    let prefix = "PS-"
    guard value.hasPrefix(prefix),
          value == value.uppercased(),
          let parsed = UUID(uuidString: String(value.dropFirst(prefix.count))),
          parsed.uuidString == String(value.dropFirst(prefix.count)) else {
        throw BridgeFailure("validation_error", "\(field) must be PS- followed by a canonical uppercase UUID.")
    }
}

private func validateOperationIDShape(_ value: String, prefix: String = "OP", field: String = "operation_id") throws {
    let marker = prefix + "-"
    guard value.hasPrefix(marker),
          value == value.uppercased(),
          let parsed = UUID(uuidString: String(value.dropFirst(marker.count))),
          parsed.uuidString == String(value.dropFirst(marker.count)) else {
        throw BridgeFailure("validation_error", "\(field) must be \(prefix)- followed by a canonical uppercase UUID.")
    }
}

private func sha256(_ data: Data) -> String {
    let digest = SHA256.hash(data: data)
    return "sha256:" + digest.map { String(format: "%02x", $0) }.joined()
}

private func sha256(_ string: String) -> String {
    sha256(Data(string.utf8))
}

private func canonicalFingerprint(_ object: [String: Any]) throws -> String {
    sha256(try jsonData(object))
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

private func parseDateTime(_ value: String, field: String) throws -> Date {
    guard matches(value, pattern: "(?:[zZ]|[+-][0-9]{2}:[0-9]{2})$") else {
        throw BridgeFailure("validation_error", "\(field) must be RFC 3339 with an explicit UTC offset.")
    }
    if let date = utcFormatter.date(from: value) ?? utcFormatterNoFraction.date(from: value) {
        return date
    }
    throw BridgeFailure("validation_error", "\(field) is not a valid RFC 3339 timestamp.")
}

private func validateOffsetMatchesTimeZone(_ value: String, date: Date, timeZone: TimeZone, field: String) throws {
    let offsetSeconds: Int
    if value.hasSuffix("Z") || value.hasSuffix("z") {
        offsetSeconds = 0
    } else {
        let offsetText = String(value.suffix(6))
        guard offsetText.count == 6,
              let hours = Int(offsetText.dropFirst().prefix(2)),
              let minutes = Int(offsetText.suffix(2)),
              hours <= 23,
              minutes <= 59 else {
            throw BridgeFailure("validation_error", "\(field) has an invalid UTC offset.")
        }
        let sign = offsetText.first == "-" ? -1 : 1
        offsetSeconds = sign * (hours * 3600 + minutes * 60)
    }
    guard timeZone.secondsFromGMT(for: date) == offsetSeconds else {
        throw BridgeFailure("validation_error", "\(field)'s UTC offset does not match the supplied IANA timezone at that instant.", details: ["timezone": timeZone.identifier])
    }
}

private func requireMinutePrecision(_ date: Date, field: String) throws {
    let seconds = date.timeIntervalSince1970.truncatingRemainder(dividingBy: 60)
    if abs(seconds) > 0.000_001 && abs(abs(seconds) - 60) > 0.000_001 {
        throw BridgeFailure("validation_error", "\(field) must use exact minute precision with seconds set to 00.")
    }
}

private func parseTimeZone(_ value: String, field: String) throws -> TimeZone {
    guard let timeZone = TimeZone(identifier: value) else {
        throw BridgeFailure("validation_error", "\(field) must be a valid IANA timezone identifier.")
    }
    return timeZone
}

private func parseDateOnly(_ value: String, field: String) throws -> (year: Int, month: Int, day: Int) {
    guard matches(value, pattern: "^[0-9]{4}-[0-9]{2}-[0-9]{2}$") else {
        throw BridgeFailure("validation_error", "\(field) must use YYYY-MM-DD.")
    }
    let pieces = value.split(separator: "-").compactMap { Int($0) }
    guard pieces.count == 3 else {
        throw BridgeFailure("validation_error", "\(field) must use YYYY-MM-DD.")
    }
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = TimeZone(secondsFromGMT: 0)!
    var components = DateComponents()
    components.calendar = calendar
    components.timeZone = calendar.timeZone
    components.year = pieces[0]
    components.month = pieces[1]
    components.day = pieces[2]
    guard let date = calendar.date(from: components) else {
        throw BridgeFailure("validation_error", "\(field) is not a real calendar date.")
    }
    let verified = calendar.dateComponents([.year, .month, .day], from: date)
    guard verified.year == pieces[0], verified.month == pieces[1], verified.day == pieces[2] else {
        throw BridgeFailure("validation_error", "\(field) is not a real calendar date.")
    }
    return (pieces[0], pieces[1], pieces[2])
}

private func defaultAllDayCalendar() -> Calendar {
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = TimeZone.current
    return calendar
}

private func allDayDate(
    year: Int,
    month: Int,
    day: Int,
    calendar: Calendar,
    field: String
) throws -> Date {
    var components = DateComponents()
    components.calendar = calendar
    components.timeZone = calendar.timeZone
    components.year = year
    components.month = month
    components.day = day
    guard let date = calendar.date(from: components) else {
        throw BridgeFailure("validation_error", "\(field) is not a real calendar date.")
    }
    return date
}

private func allDayDateString(_ date: Date, calendar: Calendar = defaultAllDayCalendar()) -> String {
    let components = calendar.dateComponents([.year, .month, .day], from: date)
    return String(
        format: "%04d-%02d-%02d",
        components.year ?? -1,
        components.month ?? -1,
        components.day ?? -1
    )
}

private struct ManagedMetadata: Equatable {
    let schemaVersion: Int
    let scheduleID: String
    let entity: Entity
    let role: String

    init(input: [String: Any], entity: Entity) throws {
        try ensureAllowedKeys(input, ["schema_version", "schedule_id", "entity", "role"], context: "managed")
        let schemaVersion = try requiredInt(input, "schema_version", context: "managed")
        guard schemaVersion == managedSchemaVersion else {
            throw BridgeFailure("unsupported_schema", "managed.schema_version is not supported.", details: ["supported": managedSchemaVersion])
        }
        let scheduleID = try requiredString(input, "schedule_id", context: "managed")
        let markerEntity = try Entity.parse(try requiredString(input, "entity", context: "managed"))
        let role = try requiredString(input, "role", context: "managed")
        guard markerEntity == entity else {
            throw BridgeFailure("validation_error", "managed.entity must match request.entity.")
        }
        try ManagedMetadata.validate(schemaVersion: schemaVersion, scheduleID: scheduleID, entity: entity, role: role)
        self.schemaVersion = schemaVersion
        self.scheduleID = scheduleID
        self.entity = entity
        self.role = role
    }

    init(blockValues: [String: String]) throws {
        let allowed: Set<String> = ["schedule_id", "entity", "role"]
        let unknown = Set(blockValues.keys).subtracting(allowed).sorted()
        guard unknown.isEmpty else {
            throw BridgeFailure("marker_malformed", "Managed metadata contains unknown key(s).", details: ["keys": unknown])
        }
        guard let scheduleID = blockValues["schedule_id"],
              let entityValue = blockValues["entity"],
              let role = blockValues["role"] else {
            throw BridgeFailure("marker_malformed", "Managed metadata is missing a required key.")
        }
        let entity = try Entity.parse(entityValue)
        try ManagedMetadata.validate(schemaVersion: managedSchemaVersion, scheduleID: scheduleID, entity: entity, role: role)
        self.schemaVersion = managedSchemaVersion
        self.scheduleID = scheduleID
        self.entity = entity
        self.role = role
    }

    private static func validate(
        schemaVersion: Int,
        scheduleID: String,
        entity: Entity,
        role: String
    ) throws {
        guard schemaVersion == managedSchemaVersion else {
            throw BridgeFailure("unsupported_schema", "Managed schema version is not supported.")
        }
        try validateScheduleIDShape(scheduleID, field: "managed.schedule_id")
        let allowedRoles: Set<String>
        switch entity {
        case .reminder: allowedRoles = ["task", "deadline"]
        case .event: allowedRoles = ["appointment", "commitment", "time-block"]
        }
        guard allowedRoles.contains(role) else {
            throw BridgeFailure("validation_error", "managed.role is not valid for this entity.", details: ["allowed": allowedRoles.sorted()])
        }
        for (field, value) in [
            ("managed.schedule_id", scheduleID),
            ("managed.entity", entity.rawValue),
            ("managed.role", role)
        ] {
            try rejectLineBreaks(value, field: field)
        }
    }

    var jsonObject: [String: Any] {
        [
            "schema_version": schemaVersion,
            "schedule_id": scheduleID,
            "entity": entity.rawValue,
            "role": role
        ]
    }

    var block: String {
        [
            managedStartMarker,
            "schedule_id=\(scheduleID)",
            "entity=\(entity.rawValue)",
            "role=\(role)",
            managedEndMarker
        ]
        .joined(separator: "\n")
    }
}

private struct ParsedManagedBlock {
    let metadata: ManagedMetadata
    let range: Range<String.Index>
}

private func allRanges(of needle: String, in haystack: String) -> [Range<String.Index>] {
    var ranges: [Range<String.Index>] = []
    var searchRange = haystack.startIndex..<haystack.endIndex
    while let range = haystack.range(of: needle, range: searchRange) {
        ranges.append(range)
        searchRange = range.upperBound..<haystack.endIndex
    }
    return ranges
}

private func parseManagedBlock(_ notes: String?) throws -> ParsedManagedBlock? {
    let text = notes ?? ""
    let starts = allRanges(of: managedStartMarker, in: text)
    let versionedStarts = allRanges(of: "[personal-scheduler:", in: text)
    let ends = allRanges(of: managedEndMarker, in: text)
    if versionedStarts.count != starts.count {
        throw BridgeFailure(
            "unsupported_schema",
            "Managed metadata uses an unsupported marker version.",
            details: ["supported_marker": managedStartMarker, "managed_metadata_schema_version": managedSchemaVersion]
        )
    }
    if starts.isEmpty && ends.isEmpty {
        if text.contains("[/personal-scheduler]") {
            throw BridgeFailure("marker_malformed", "Managed metadata markers are damaged.")
        }
        return nil
    }
    guard starts.count == 1, ends.count == 1, starts[0].lowerBound < ends[0].lowerBound else {
        throw BridgeFailure("marker_malformed", "Managed metadata must contain exactly one well-ordered marker block.")
    }
    let bodyStart = starts[0].upperBound
    let bodyEnd = ends[0].lowerBound
    let body = String(text[bodyStart..<bodyEnd])
    var values: [String: String] = [:]
    for rawLine in body.split(whereSeparator: { $0.isNewline }) {
        let line = String(rawLine).trimmingCharacters(in: .whitespaces)
        if line.isEmpty { continue }
        guard let separator = line.firstIndex(of: "=") else {
            throw BridgeFailure("marker_malformed", "Every managed metadata line must use key=value.")
        }
        let key = String(line[..<separator])
        let value = String(line[line.index(after: separator)...])
        guard !key.isEmpty, !value.isEmpty, values[key] == nil else {
            throw BridgeFailure("marker_malformed", "Managed metadata contains an empty or duplicate key.")
        }
        values[key] = value
    }
    let metadata = try ManagedMetadata(blockValues: values)
    return ParsedManagedBlock(metadata: metadata, range: starts[0].lowerBound..<ends[0].upperBound)
}

private func mergeManagedBlock(existingNotes: String?, metadata: ManagedMetadata) throws -> String {
    let text = existingNotes ?? ""
    if let parsed = try parseManagedBlock(text) {
        return text.replacingCharacters(in: parsed.range, with: metadata.block)
    }
    if text.isEmpty { return metadata.block }
    return text + (text.hasSuffix("\n") ? "\n" : "\n\n") + metadata.block
}

private func externalNotesPresent(_ notes: String?, parsed: ParsedManagedBlock?) -> Bool {
    guard let notes, let parsed else { return !(notes?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ?? true) }
    var remainder = notes
    remainder.removeSubrange(parsed.range)
    return !remainder.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
}

private struct DateWindow {
    let start: Date
    let end: Date

    init(input: [String: Any], context: String, maximumDays: Double) throws {
        try ensureAllowedKeys(input, ["start_at", "end_at"], context: context)
        start = try parseDateTime(try requiredString(input, "start_at", context: context), field: "\(context).start_at")
        end = try parseDateTime(try requiredString(input, "end_at", context: context), field: "\(context).end_at")
        guard start < end else {
            throw BridgeFailure("validation_error", "\(context).start_at must be earlier than end_at.")
        }
        guard end.timeIntervalSince(start) <= maximumDays * 86_400 else {
            throw BridgeFailure("validation_error", "\(context) exceeds the supported window.", details: ["maximum_days": maximumDays])
        }
    }

    var jsonObject: [String: Any] {
        ["start_at": isoString(start), "end_at": isoString(end)]
    }
}

private enum ReminderDue {
    case none
    case date(year: Int, month: Int, day: Int)
    case dateTime(date: Date, timeZone: TimeZone)

    init(input: [String: Any]) throws {
        let kind = try requiredString(input, "kind", context: "payload.due")
        switch kind {
        case "none":
            try ensureAllowedKeys(input, ["kind"], context: "payload.due")
            self = .none
        case "date":
            try ensureAllowedKeys(input, ["kind", "date"], context: "payload.due")
            let parsed = try parseDateOnly(try requiredString(input, "date", context: "payload.due"), field: "payload.due.date")
            self = .date(year: parsed.year, month: parsed.month, day: parsed.day)
        case "date_time":
            try ensureAllowedKeys(input, ["kind", "at", "timezone"], context: "payload.due")
            let at = try requiredString(input, "at", context: "payload.due")
            let date = try parseDateTime(at, field: "payload.due.at")
            let timeZone = try parseTimeZone(try requiredString(input, "timezone", context: "payload.due"), field: "payload.due.timezone")
            try validateOffsetMatchesTimeZone(at, date: date, timeZone: timeZone, field: "payload.due.at")
            try requireMinutePrecision(date, field: "payload.due.at")
            self = .dateTime(date: date, timeZone: timeZone)
        default:
            throw BridgeFailure("validation_error", "payload.due.kind must be none, date, or date_time.")
        }
    }

    var dateComponents: DateComponents? {
        switch self {
        case .none:
            return nil
        case let .date(year, month, day):
            var components = DateComponents()
            components.calendar = Calendar(identifier: .gregorian)
            components.year = year
            components.month = month
            components.day = day
            return components
        case let .dateTime(date, timeZone):
            var calendar = Calendar(identifier: .gregorian)
            calendar.timeZone = timeZone
            var components = calendar.dateComponents([.year, .month, .day, .hour, .minute, .second], from: date)
            components.calendar = calendar
            components.timeZone = timeZone
            return components
        }
    }

    var jsonObject: [String: Any] {
        switch self {
        case .none:
            return ["kind": "none"]
        case let .date(year, month, day):
            return ["kind": "date", "date": String(format: "%04d-%02d-%02d", year, month, day)]
        case let .dateTime(date, timeZone):
            return ["kind": "date_time", "at": isoString(date), "timezone": timeZone.identifier]
        }
    }
}

private enum EventTiming {
    case timed(start: Date, end: Date, timeZone: TimeZone)
    case allDay(start: Date, end: Date, startText: String, endText: String)

    init(input: [String: Any]) throws {
        let kind = try requiredString(input, "kind", context: "payload.time")
        switch kind {
        case "timed":
            try ensureAllowedKeys(input, ["kind", "start_at", "end_at", "timezone"], context: "payload.time")
            let startText = try requiredString(input, "start_at", context: "payload.time")
            let endText = try requiredString(input, "end_at", context: "payload.time")
            let start = try parseDateTime(startText, field: "payload.time.start_at")
            let end = try parseDateTime(endText, field: "payload.time.end_at")
            guard start < end else {
                throw BridgeFailure("validation_error", "payload.time.start_at must be earlier than end_at.")
            }
            guard end.timeIntervalSince(start) <= 7 * 86_400 else {
                throw BridgeFailure("validation_error", "A managed timed event cannot exceed seven days.")
            }
            let timeZone = try parseTimeZone(try requiredString(input, "timezone", context: "payload.time"), field: "payload.time.timezone")
            try validateOffsetMatchesTimeZone(startText, date: start, timeZone: timeZone, field: "payload.time.start_at")
            try validateOffsetMatchesTimeZone(endText, date: end, timeZone: timeZone, field: "payload.time.end_at")
            try requireMinutePrecision(start, field: "payload.time.start_at")
            try requireMinutePrecision(end, field: "payload.time.end_at")
            self = .timed(start: start, end: end, timeZone: timeZone)
        case "all_day":
            try ensureAllowedKeys(input, ["kind", "start_date", "end_date_exclusive"], context: "payload.time")
            let startText = try requiredString(input, "start_date", context: "payload.time")
            let endText = try requiredString(input, "end_date_exclusive", context: "payload.time")
            let startParts = try parseDateOnly(startText, field: "payload.time.start_date")
            let endParts = try parseDateOnly(endText, field: "payload.time.end_date_exclusive")
            let calendar = defaultAllDayCalendar()
            let start = try allDayDate(
                year: startParts.year,
                month: startParts.month,
                day: startParts.day,
                calendar: calendar,
                field: "payload.time.start_date"
            )
            let end = try allDayDate(
                year: endParts.year,
                month: endParts.month,
                day: endParts.day,
                calendar: calendar,
                field: "payload.time.end_date_exclusive"
            )
            guard start < end else {
                throw BridgeFailure("validation_error", "payload.time all-day dates are invalid or empty.")
            }
            guard let dayCount = calendar.dateComponents([.day], from: start, to: end).day, dayCount <= 31 else {
                throw BridgeFailure("validation_error", "A managed all-day event cannot exceed 31 days.")
            }
            self = .allDay(start: start, end: end, startText: startText, endText: endText)
        default:
            throw BridgeFailure("validation_error", "payload.time.kind must be timed or all_day.")
        }
    }

    var start: Date {
        switch self {
        case let .timed(start, _, _), let .allDay(start, _, _, _): return start
        }
    }

    var end: Date {
        switch self {
        case let .timed(_, end, _), let .allDay(_, end, _, _): return end
        }
    }

    var timeZone: TimeZone? {
        switch self {
        case let .timed(_, _, timeZone): return timeZone
        case .allDay: return nil
        }
    }

    var allDay: Bool {
        if case .allDay = self { return true }
        return false
    }

    var jsonObject: [String: Any] {
        switch self {
        case let .timed(start, end, timeZone):
            return ["kind": "timed", "start_at": isoString(start), "end_at": isoString(end), "timezone": timeZone.identifier]
        case let .allDay(_, _, startText, endText):
            return ["kind": "all_day", "start_date": startText, "end_date_exclusive": endText]
        }
    }
}

private struct ReminderPayload {
    let title: String
    let due: ReminderDue
    let priority: Int
    let userNotes: String?

    init(input: [String: Any], allowUserNotes: Bool) throws {
        var allowed: Set<String> = ["title", "due", "priority"]
        if allowUserNotes { allowed.insert("user_notes") }
        try ensureAllowedKeys(input, allowed, context: "payload")
        title = try requiredString(input, "title", context: "payload").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty, title.count <= 500 else {
            throw BridgeFailure("validation_error", "payload.title must contain 1–500 characters.")
        }
        try rejectLineBreaks(title, field: "payload.title")
        due = try ReminderDue(input: try requiredObject(input, "due", context: "payload"))
        priority = try requiredInt(input, "priority", context: "payload")
        guard [0, 1, 5, 9].contains(priority) else {
            throw BridgeFailure("validation_error", "payload.priority must be 0, 1, 5, or 9.")
        }
        userNotes = allowUserNotes ? try optionalString(input, "user_notes", context: "payload") : nil
    }

    var safeJSON: [String: Any] {
        ["title": title, "due": due.jsonObject, "priority": priority, "has_user_notes": !(userNotes?.isEmpty ?? true)]
    }
}

private struct EventPayload {
    let title: String
    let location: String?
    let timing: EventTiming
    let alarmMinutesBefore: [Int]
    let userNotes: String?

    init(input: [String: Any], allowUserNotes: Bool) throws {
        var allowed: Set<String> = ["title", "location", "time", "alarms"]
        if allowUserNotes { allowed.insert("user_notes") }
        try ensureAllowedKeys(input, allowed, context: "payload")
        title = try requiredString(input, "title", context: "payload").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty, title.count <= 500 else {
            throw BridgeFailure("validation_error", "payload.title must contain 1–500 characters.")
        }
        try rejectLineBreaks(title, field: "payload.title")
        guard input.keys.contains("location") else {
            throw BridgeFailure("validation_error", "payload.location is required; use null when no location is intended.")
        }
        let parsedLocation = try optionalString(input, "location", context: "payload")
        if let parsedLocation {
            guard !parsedLocation.isEmpty, !parsedLocation.contains("\0") else {
                throw BridgeFailure("validation_error", "payload.location must be a non-empty string without NUL, or null; use null for no location.")
            }
        }
        location = parsedLocation
        timing = try EventTiming(input: try requiredObject(input, "time", context: "payload"))
        guard let rawAlarms = input["alarms"] as? [Any] else {
            throw BridgeFailure("validation_error", "payload.alarms must be an array.")
        }
        guard rawAlarms.count <= 5 else {
            throw BridgeFailure("validation_error", "payload.alarms supports at most five entries.")
        }
        alarmMinutesBefore = try rawAlarms.map { raw in
            guard let object = raw as? [String: Any] else {
                throw BridgeFailure("validation_error", "Each payload.alarms entry must be an object.")
            }
            try ensureAllowedKeys(object, ["minutes_before"], context: "payload.alarms[]")
            let minutes = try requiredInt(object, "minutes_before", context: "payload.alarms[]")
            guard (0...10_080).contains(minutes) else {
                throw BridgeFailure("validation_error", "minutes_before must be between 0 and 10080.")
            }
            return minutes
        }
        guard Set(alarmMinutesBefore).count == alarmMinutesBefore.count else {
            throw BridgeFailure("validation_error", "payload.alarms must not contain duplicates.")
        }
        userNotes = allowUserNotes ? try optionalString(input, "user_notes", context: "payload") : nil
    }

    var safeJSON: [String: Any] {
        [
            "title": title,
            "location": location ?? NSNull(),
            "time": timing.jsonObject,
            "alarms": alarmMinutesBefore.sorted().map { ["minutes_before": $0] },
            "has_user_notes": !(userNotes?.isEmpty ?? true)
        ]
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

private func entityMaskNames(_ mask: EKEntityMask) -> [String] {
    var names: [String] = []
    if mask.contains(.event) { names.append("event") }
    if mask.contains(.reminder) { names.append("reminder") }
    return names
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
            throw BridgeFailure(code, "Full \(entity.rawValue) access is required for structured readback and reconciliation.", details: ["status": authorizationName(status)])
        }
    }

    func source(identifier: String, requireCalDAV: Bool) throws -> EKSource {
        guard let source = store.source(withIdentifier: identifier) else {
            throw BridgeFailure("source_missing", "The requested EventKit source is unavailable.", details: ["source_id": identifier])
        }
        if requireCalDAV, source.sourceType != .calDAV {
            throw BridgeFailure("source_not_caldav", "Managed schedules require the user-confirmed iCloud CalDAV source; local/default fallback is forbidden.", details: ["source_id": identifier, "source_type": sourceTypeName(source.sourceType)])
        }
        return source
    }

    func container(identifier: String, entity: Entity, sourceID: String, writable: Bool, requireCalDAV: Bool = true) throws -> EKCalendar {
        guard let calendar = store.calendar(withIdentifier: identifier) else {
            throw BridgeFailure("container_missing", "The requested EventKit container is unavailable.", details: ["container_id": identifier])
        }
        guard calendar.source.sourceIdentifier == sourceID else {
            throw BridgeFailure("container_source_mismatch", "The container no longer belongs to the expected source.", details: ["container_id": identifier, "expected_source_id": sourceID, "actual_source_id": calendar.source.sourceIdentifier])
        }
        _ = try source(identifier: sourceID, requireCalDAV: requireCalDAV)
        let expectedMask: EKEntityMask = entity == .event ? .event : .reminder
        guard calendar.allowedEntityTypes.contains(expectedMask) else {
            throw BridgeFailure("container_type_mismatch", "The container does not support the requested entity type.", details: ["container_id": identifier, "entity": entity.rawValue])
        }
        if writable {
            guard calendar.allowsContentModifications else {
                throw BridgeFailure("container_not_writable", "The requested EventKit container is not writable.", details: ["container_id": identifier])
            }
            guard !calendar.isSubscribed, !calendar.isImmutable, !calendar.source.isDelegate else {
                throw BridgeFailure("container_risk", "Subscribed, immutable, or delegated containers are read-only for personal-scheduler mutations.", details: ["container_id": identifier])
            }
        }
        return calendar
    }
}

private func requirePrivateWriteConfirmation(_ input: [String: Any]) throws {
    guard try requiredBool(input, "confirm_private_container") else {
        throw BridgeFailure("confirmation_required", "The caller must explicitly confirm that the selected write container is private and intended for personal-scheduler.")
    }
}

private func mutationExpectedStoreID(_ input: [String: Any], dryRun: Bool) throws -> String? {
    if dryRun {
        guard input["expected_event_store_id"] == nil else {
            throw BridgeFailure(
                "validation_error",
                "expected_event_store_id is reserved for the executor's actual mutation call."
            )
        }
        return nil
    }
    return try requiredString(input, "expected_event_store_id")
}

private func requireExpectedStoreID(_ expected: String?, context: StoreContext) throws {
    guard let expected else { return }
    guard context.store.eventStoreIdentifier == expected else {
        throw BridgeFailure(
            "event_store_changed_before_mutation",
            "The EventKit store changed after preflight; no mutation was attempted."
        )
    }
}

private func sourceJSON(_ source: EKSource, entity: Entity? = nil) -> [String: Any] {
    var object: [String: Any] = [
        "source_id": source.sourceIdentifier,
        "title": source.title,
        "source_type": sourceTypeName(source.sourceType),
        "is_delegate": source.isDelegate
    ]
    if let entity {
        object["container_count"] = source.calendars(for: entity.eventKitType).count
    }
    return object
}

private func containerJSON(_ calendar: EKCalendar) -> [String: Any] {
    [
        "container_id": calendar.calendarIdentifier,
        "title": calendar.title,
        "source_id": calendar.source.sourceIdentifier,
        "source_title": calendar.source.title,
        "source_type": sourceTypeName(calendar.source.sourceType),
        "writable": calendar.allowsContentModifications,
        "subscribed": calendar.isSubscribed,
        "immutable": calendar.isImmutable,
        "source_is_delegate": calendar.source.isDelegate,
        "sharing_state": "unknown_eventkit",
        "allowed_entities": entityMaskNames(calendar.allowedEntityTypes)
    ]
}

private func componentsJSON(_ components: DateComponents?) -> Any {
    guard let components else { return NSNull() }
    var object: [String: Any] = [:]
    if let calendar = components.calendar { object["calendar"] = calendar.identifier.debugDescription }
    if let timeZone = components.timeZone { object["timezone"] = timeZone.identifier }
    if let era = components.era { object["era"] = era }
    if let year = components.year { object["year"] = year }
    if let month = components.month { object["month"] = month }
    if let day = components.day { object["day"] = day }
    if let hour = components.hour { object["hour"] = hour }
    if let minute = components.minute { object["minute"] = minute }
    if let second = components.second { object["second"] = second }
    return object
}

private func alarmFingerprintJSON(_ alarm: EKAlarm) -> [String: Any] {
    var object: [String: Any] = [
        "type": alarm.type.rawValue,
        "proximity": alarm.proximity.rawValue,
        "email_address": alarm.emailAddress ?? NSNull(),
        "sound_name": alarm.soundName ?? NSNull()
    ]
    if let absoluteDate = alarm.absoluteDate {
        object["kind"] = "absolute"
        object["absolute_at"] = isoString(absoluteDate)
    } else {
        object["kind"] = "relative"
        object["relative_seconds"] = alarm.relativeOffset
    }
    object["structured_location"] = structuredLocationJSON(alarm.structuredLocation)
    return object
}

private func sortedAlarms(_ alarms: [EKAlarm]?) -> [[String: Any]] {
    let values = (alarms ?? []).map(alarmFingerprintJSON)
    return values.sorted { left, right in
        let leftData = (try? jsonData(left)) ?? Data()
        let rightData = (try? jsonData(right)) ?? Data()
        return leftData.lexicographicallyPrecedes(rightData)
    }
}

private func itemIdentifier(_ item: EKCalendarItem) -> String {
    if let event = item as? EKEvent {
        return event.eventIdentifier ?? event.calendarItemIdentifier
    }
    return item.calendarItemIdentifier
}

private func assertNonRecurring(_ item: EKCalendarItem) throws {
    let detachedOccurrence = (item as? EKEvent)?.isDetached ?? false
    if item.hasRecurrenceRules || !(item.recurrenceRules?.isEmpty ?? true) || detachedOccurrence {
        throw BridgeFailure(
            "unsupported_recurrence",
            "Managed recurring reminders and events are outside this bridge contract; mutation is fail-closed.",
            details: ["item_id": itemIdentifier(item)]
        )
    }
}

private final class ProcessLock {
    private let descriptor: Int32

    init() throws {
        guard let caches = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first else {
            throw BridgeFailure("lock_unavailable", "The user cache directory is unavailable.")
        }
        let directory = caches.appendingPathComponent(expectedBundleIdentifier, isDirectory: true)
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        } catch {
            throw BridgeFailure("lock_unavailable", "The bridge could not create its private lock directory.")
        }
        let directoryDescriptor = Darwin.open(directory.path, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW)
        guard directoryDescriptor >= 0 else {
            throw BridgeFailure("lock_unavailable", "The private lock directory could not be opened safely.", details: ["errno": errno])
        }
        defer { Darwin.close(directoryDescriptor) }
        var directoryStatus = stat()
        guard fstat(directoryDescriptor, &directoryStatus) == 0,
              directoryStatus.st_uid == getuid(),
              (directoryStatus.st_mode & S_IFMT) == S_IFDIR,
              (directoryStatus.st_mode & 0o777) == 0o700 else {
            throw BridgeFailure("lock_unavailable", "The private lock directory failed owner, type, symlink, or 0700 mode validation.")
        }
        descriptor = Darwin.openat(directoryDescriptor, "eventkit.lock", O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, S_IRUSR | S_IWUSR)
        guard descriptor >= 0 else {
            throw BridgeFailure("lock_unavailable", "The bridge could not open its process lock.", details: ["errno": errno])
        }
        var status = stat()
        guard fstat(descriptor, &status) == 0,
              status.st_uid == getuid(),
              (status.st_mode & S_IFMT) == S_IFREG,
              (status.st_mode & 0o777) == 0o600 else {
            Darwin.close(descriptor)
            throw BridgeFailure("lock_unavailable", "The process lock failed ownership or type validation.")
        }
        let deadline = Date().addingTimeInterval(10)
        while flock(descriptor, LOCK_EX | LOCK_NB) != 0 {
            guard errno == EWOULDBLOCK || errno == EAGAIN else {
                Darwin.close(descriptor)
                throw BridgeFailure("lock_unavailable", "The bridge could not acquire its process lock.", details: ["errno": errno])
            }
            guard Date() < deadline else {
                Darwin.close(descriptor)
                throw BridgeFailure("lock_timeout", "Another bridge mutation is still in progress; no mutation was attempted.")
            }
            usleep(50_000)
        }
    }

    deinit {
        _ = flock(descriptor, LOCK_UN)
        Darwin.close(descriptor)
    }
}

private func withProcessLock(_ work: () throws -> Void) throws {
    let lock = try ProcessLock()
    try withExtendedLifetime(lock) {
        try work()
    }
}

private func structuredLocationJSON(_ structured: EKStructuredLocation?) -> Any {
    guard let structured else { return NSNull() }
    var location: [String: Any] = [
        "title": structured.title ?? NSNull(),
        "radius": structured.radius
    ]
    if let coordinate = structured.geoLocation?.coordinate {
        location["latitude"] = coordinate.latitude
        location["longitude"] = coordinate.longitude
    }
    return location
}

private func eventInvitationJSON(_ event: EKEvent) -> [String: Any] {
    [
        "has_attendees": event.hasAttendees,
        "attendee_count": event.attendees?.count ?? 0,
        "organizer_present": event.organizer != nil
    ]
}

private func eventHasInvitation(_ event: EKEvent) -> Bool {
    event.hasAttendees || !(event.attendees?.isEmpty ?? true) || event.organizer != nil
}

private struct OwnershipInfo {
    let classification: String
    let parsed: ParsedManagedBlock?
}

private func ownershipInfo(_ notes: String?) -> OwnershipInfo {
    let text = notes ?? ""
    let hasOwnSyntax = text.contains("[personal-scheduler:") || text.contains(managedEndMarker) || text.contains("[/personal-scheduler]")
    let hasGoalSyntax = text.contains("[goal-planner:") || text.contains("[/goal-planner]")
    // Treat any standalone bracketed namespace-like line as managed syntax.
    // Exact own/Goal markers are allowlisted; case variants, underscores,
    // dots, unknown versions, and unknown namespaces are foreign and read-only.
    let markerPattern = #"(?m)^\s*\[/?[A-Za-z0-9][A-Za-z0-9_.-]*(?::[A-Za-z0-9_.-]+)?\]\s*$"#
    let expression = try? NSRegularExpression(pattern: markerPattern)
    let string = text as NSString
    let markers = expression?.matches(in: text, range: NSRange(location: 0, length: string.length)).map {
        string.substring(with: $0.range)
    } ?? []
    let knownMarkers: Set<String> = [managedStartMarker, managedEndMarker, "[goal-planner:v2]", "[/goal-planner]"]
    let hasForeignSyntax = markers.contains { !knownMarkers.contains($0) }

    if hasOwnSyntax {
        do {
            guard let parsed = try parseManagedBlock(text) else {
                return OwnershipInfo(classification: "malformed_marker", parsed: nil)
            }
            guard !hasGoalSyntax, !hasForeignSyntax else {
                return OwnershipInfo(classification: "malformed_marker", parsed: nil)
            }
            return OwnershipInfo(classification: "personal_scheduler", parsed: parsed)
        } catch {
            return OwnershipInfo(classification: "malformed_marker", parsed: nil)
        }
    }
    if hasGoalSyntax && !hasForeignSyntax {
        return OwnershipInfo(classification: "goal_planner", parsed: nil)
    }
    if hasGoalSyntax || hasForeignSyntax {
        return OwnershipInfo(classification: "foreign_marker", parsed: nil)
    }
    return OwnershipInfo(classification: "unmanaged", parsed: nil)
}

private func alarmSafetyJSON(_ item: EKCalendarItem) -> [String: Any] {
    let alarms = item.alarms ?? []
    return [
        "count": alarms.count,
        "has_email": alarms.contains { $0.emailAddress != nil },
        "has_sound": alarms.contains { $0.soundName != nil },
        "has_location_trigger": alarms.contains { $0.structuredLocation != nil || $0.proximity != .none },
        "has_absolute_trigger": alarms.contains { $0.absoluteDate != nil }
    ]
}

private func rawFingerprintObject(_ item: EKCalendarItem, entity: Entity, ownership: OwnershipInfo) -> [String: Any] {
    var object: [String: Any] = [
        "entity": entity.rawValue,
        "container_id": item.calendar.calendarIdentifier,
        "source_id": item.calendar.source.sourceIdentifier,
        "title": item.title ?? "",
        "location": item.location ?? NSNull(),
        "url": item.url?.absoluteString ?? NSNull(),
        "notes": item.notes ?? "",
        "ownership": ownership.classification,
        "alarms": sortedAlarms(item.alarms),
        "recurrence_count": item.recurrenceRules?.count ?? 0,
        "detached": (item as? EKEvent)?.isDetached ?? false
    ]
    if let managed = ownership.parsed?.metadata.jsonObject { object["managed"] = managed }
    if let reminder = item as? EKReminder {
        object["timezone"] = reminder.timeZone?.identifier ?? NSNull()
        object["start_components"] = componentsJSON(reminder.startDateComponents)
        object["due_components"] = componentsJSON(reminder.dueDateComponents)
        object["completed"] = reminder.isCompleted
        object["completion_at"] = isoString(reminder.completionDate)
        object["priority"] = reminder.priority
    } else if let event = item as? EKEvent {
        object["all_day"] = event.isAllDay
        if event.isAllDay {
            let calendar = defaultAllDayCalendar()
            object["start_date"] = allDayDateString(event.startDate, calendar: calendar)
            object["end_date_exclusive"] = allDayDateString(event.endDate, calendar: calendar)
        } else {
            object["timezone"] = event.timeZone?.identifier ?? NSNull()
            object["start_at"] = isoString(event.startDate)
            object["end_at"] = isoString(event.endDate)
        }
        object["availability"] = event.availability.rawValue
        object["status"] = event.status.rawValue
        object["structured_location"] = structuredLocationJSON(event.structuredLocation)
        object["invitation"] = eventInvitationJSON(event)
    }
    return object
}

private func reminderContentObject(_ reminder: EKReminder) throws -> [String: Any] {
    let due: [String: Any]
    if let components = reminder.dueDateComponents {
        if components.hour == nil && components.minute == nil && components.second == nil {
            guard let year = components.year, let month = components.month, let day = components.day else {
                throw BridgeFailure("readback_mismatch", "A date-only Reminder has incomplete date components.")
            }
            due = ["kind": "date", "date": String(format: "%04d-%02d-%02d", year, month, day)]
        } else {
            let timeZone = components.timeZone ?? reminder.timeZone ?? TimeZone.current
            var calendar = components.calendar ?? Calendar(identifier: .gregorian)
            calendar.timeZone = timeZone
            guard let date = calendar.date(from: components) else {
                throw BridgeFailure("readback_mismatch", "A timed Reminder has invalid due components.")
            }
            due = ["kind": "date_time", "at": isoString(date), "timezone": timeZone.identifier]
        }
    } else {
        due = ["kind": "none"]
    }
    return ["title": reminder.title ?? "", "due": due, "priority": reminder.priority]
}

private func eventContentObject(_ event: EKEvent) -> [String: Any] {
    let timing: [String: Any]
    if event.isAllDay {
        let calendar = defaultAllDayCalendar()
        timing = [
            "kind": "all_day",
            "start_date": allDayDateString(event.startDate, calendar: calendar),
            "end_date_exclusive": allDayDateString(event.endDate, calendar: calendar)
        ]
    } else {
        timing = [
            "kind": "timed",
            "start_at": isoString(event.startDate),
            "end_at": isoString(event.endDate),
            "timezone": event.timeZone?.identifier ?? NSNull()
        ]
    }
    let alarms: Any
    if let minutes = relativeAlarmMinutes(event) {
        alarms = minutes.map { ["minutes_before": $0] }
    } else {
        alarms = NSNull()
    }
    return [
        "title": event.title ?? "",
        "location": event.location ?? NSNull(),
        "time": timing,
        "alarms": alarms
    ]
}

private func currentContentHash(_ item: EKCalendarItem, entity: Entity) throws -> String {
    switch entity {
    case .reminder:
        guard let reminder = item as? EKReminder else {
            throw BridgeFailure("item_type_mismatch", "The item is not a Reminder.")
        }
        return try canonicalFingerprint(try reminderContentObject(reminder))
    case .event:
        guard let event = item as? EKEvent else {
            throw BridgeFailure("item_type_mismatch", "The item is not a Calendar event.")
        }
        return try canonicalFingerprint(eventContentObject(event))
    }
}

private func safeSnapshot(_ item: EKCalendarItem, entity: Entity) throws -> [String: Any] {
    let ownership = ownershipInfo(item.notes)
    let raw = rawFingerprintObject(item, entity: entity, ownership: ownership)
    let fingerprint = try canonicalFingerprint(raw)
    var snapshot: [String: Any] = [
        "entity": entity.rawValue,
        "item_id": itemIdentifier(item),
        "external_id": item.calendarItemExternalIdentifier ?? NSNull(),
        "container_id": item.calendar.calendarIdentifier,
        "source_id": item.calendar.source.sourceIdentifier,
        "title": item.title ?? "",
        "ownership": ownership.classification,
        "has_notes": !(item.notes?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ?? true),
        "has_external_notes": externalNotesPresent(item.notes, parsed: ownership.parsed),
        "has_url": item.url != nil,
        "last_modified_at": isoString(item.lastModifiedDate),
        "created_at": isoString(item.creationDate),
        "recurring": item.hasRecurrenceRules || !(item.recurrenceRules?.isEmpty ?? true) || ((item as? EKEvent)?.isDetached ?? false),
        "alarm_safety": alarmSafetyJSON(item),
        "container_risk": [
            "writable": item.calendar.allowsContentModifications,
            "subscribed": item.calendar.isSubscribed,
            "immutable": item.calendar.isImmutable,
            "source_is_delegate": item.calendar.source.isDelegate,
            "sharing_state": "unknown_eventkit"
        ],
        "fingerprint": fingerprint,
        "content_hash": try currentContentHash(item, entity: entity)
    ]
    if let managed = ownership.parsed?.metadata.jsonObject { snapshot["managed"] = managed }
    if let reminder = item as? EKReminder {
        snapshot["timezone"] = reminder.timeZone?.identifier ?? NSNull()
        snapshot["start_components"] = componentsJSON(reminder.startDateComponents)
        snapshot["due_components"] = componentsJSON(reminder.dueDateComponents)
        snapshot["completed"] = reminder.isCompleted
        snapshot["completion_at"] = isoString(reminder.completionDate)
        snapshot["priority"] = reminder.priority
    } else if let event = item as? EKEvent {
        snapshot["all_day"] = event.isAllDay
        if event.isAllDay {
            let calendar = defaultAllDayCalendar()
            snapshot["start_date"] = allDayDateString(event.startDate, calendar: calendar)
            snapshot["end_date_exclusive"] = allDayDateString(event.endDate, calendar: calendar)
        } else {
            snapshot["timezone"] = event.timeZone?.identifier ?? NSNull()
            snapshot["start_at"] = isoString(event.startDate)
            snapshot["end_at"] = isoString(event.endDate)
        }
        snapshot["availability"] = event.availability.rawValue
        snapshot["status"] = event.status.rawValue
        snapshot["has_location"] = !(event.location?.isEmpty ?? true)
        snapshot["location"] = event.location ?? NSNull()
        snapshot["has_structured_location"] = event.structuredLocation != nil
        snapshot["alarm_minutes_before"] = relativeAlarmMinutes(event) ?? NSNull()
        snapshot["invitation"] = eventInvitationJSON(event)
    }
    return snapshot
}

private func requireExpectedFingerprint(_ item: EKCalendarItem, entity: Entity, expected: String) throws -> [String: Any] {
    guard matches(expected, pattern: "^sha256:[0-9a-f]{64}$") else {
        throw BridgeFailure("validation_error", "expected_fingerprint must be a sha256 fingerprint returned by this bridge.")
    }
    let snapshot = try safeSnapshot(item, entity: entity)
    guard snapshot["fingerprint"] as? String == expected else {
        throw BridgeFailure(
            "stale_object",
            "The EventKit item changed after the caller's preview; mutation was refused.",
            details: ["current": snapshot]
        )
    }
    return snapshot
}

private func validateItemIdentity(
    _ item: EKCalendarItem,
    entity: Entity,
    sourceID: String,
    containerID: String,
    scheduleID: String
) throws -> ManagedMetadata {
    let actualEntity: Entity
    if item is EKReminder {
        actualEntity = .reminder
    } else if item is EKEvent {
        actualEntity = .event
    } else {
        throw BridgeFailure("item_type_mismatch", "The EventKit item has an unsupported type.")
    }
    guard actualEntity == entity else {
        throw BridgeFailure("item_type_mismatch", "The EventKit item does not match the requested entity type.")
    }
    guard item.calendar.calendarIdentifier == containerID else {
        throw BridgeFailure("item_moved", "The managed item is no longer in the expected container.", details: ["actual_container_id": item.calendar.calendarIdentifier])
    }
    guard item.calendar.source.sourceIdentifier == sourceID else {
        throw BridgeFailure("item_moved", "The managed item is no longer in the expected source.", details: ["actual_source_id": item.calendar.source.sourceIdentifier])
    }
    let ownership = ownershipInfo(item.notes)
    guard ownership.classification == "personal_scheduler", let parsed = ownership.parsed else {
        let code = ownership.classification == "unmanaged" ? "marker_missing" : "ownership_conflict"
        throw BridgeFailure(code, "The EventKit item is not exclusively owned by personal-scheduler.", details: ["ownership": ownership.classification])
    }
    guard parsed.metadata.scheduleID == scheduleID else {
        throw BridgeFailure("schedule_mismatch", "The item marker does not match the requested schedule_id.", details: ["actual_schedule_id": parsed.metadata.scheduleID])
    }
    guard parsed.metadata.entity == entity else {
        throw BridgeFailure("item_type_mismatch", "The managed marker entity does not match the EventKit item type.")
    }
    try validateScheduleIDShape(parsed.metadata.scheduleID, field: "managed.schedule_id")
    try assertNonRecurring(item)
    return parsed.metadata
}

private func fetchItem(store: EKEventStore, entity: Entity, itemID: String) -> EKCalendarItem? {
    switch entity {
    case .event:
        return store.event(withIdentifier: itemID)
    case .reminder:
        return store.calendarItem(withIdentifier: itemID) as? EKReminder
    }
}

private func fetchReminders(
    store: EKEventStore,
    calendars: [EKCalendar],
    timeoutSeconds: Int
) throws -> [EKReminder] {
    let predicate = store.predicateForReminders(in: calendars)
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
    let waitResult = semaphore.wait(timeout: .now() + .seconds(timeoutSeconds))
    if waitResult == .timedOut {
        lock.lock()
        if !terminal {
            terminal = true
            lock.unlock()
            store.cancelFetchRequest(token)
            throw BridgeFailure("timeout", "EventKit reminder fetch timed out and was cancelled.", details: ["timeout_seconds": timeoutSeconds])
        }
        lock.unlock()
    }
    lock.lock()
    let reminders = result
    lock.unlock()
    guard let reminders else {
        throw BridgeFailure("eventkit_error", "EventKit returned no reminder result.")
    }
    return reminders
}

private func findManagedMatches(
    context: StoreContext,
    entity: Entity,
    calendar: EKCalendar,
    scheduleID: String,
    searchWindow: DateWindow?,
    timeoutSeconds: Int
) throws -> [EKCalendarItem] {
    let items: [EKCalendarItem]
    switch entity {
    case .reminder:
        items = try fetchReminders(store: context.store, calendars: [calendar], timeoutSeconds: timeoutSeconds)
    case .event:
        guard let searchWindow else {
            throw BridgeFailure("validation_error", "search_window is required when finding a Calendar event.")
        }
        let predicate = context.store.predicateForEvents(withStart: searchWindow.start, end: searchWindow.end, calendars: [calendar])
        items = context.store.events(matching: predicate)
    }

    var matches: [EKCalendarItem] = []
    for item in items {
        let ownership = ownershipInfo(item.notes)
        if ownership.classification == "personal_scheduler",
           let metadata = ownership.parsed?.metadata,
           metadata.scheduleID == scheduleID {
            guard metadata.entity == entity else {
                throw BridgeFailure("item_type_mismatch", "A matching schedule_id has the wrong marker entity.")
            }
            try assertNonRecurring(item)
            matches.append(item)
        } else if (item.notes ?? "").contains(scheduleID) {
            throw BridgeFailure("ownership_conflict", "A schedule_id appears in an object whose ownership marker is not valid.", details: ["ownership": ownership.classification])
        }
    }
    return matches
}

private func uniqueManagedMatch(
    context: StoreContext,
    entity: Entity,
    calendar: EKCalendar,
    scheduleID: String,
    searchWindow: DateWindow?,
    timeoutSeconds: Int
) throws -> EKCalendarItem? {
    let matches = try findManagedMatches(
        context: context,
        entity: entity,
        calendar: calendar,
        scheduleID: scheduleID,
        searchWindow: searchWindow,
        timeoutSeconds: timeoutSeconds
    )
    if matches.count > 1 {
        let snapshots = try matches.map { try safeSnapshot($0, entity: entity) }
        throw BridgeFailure("schedule_duplicate", "More than one non-recurring item has the same schedule_id.", details: ["count": matches.count, "items": snapshots])
    }
    return matches.first
}

private func loadManagedItem(
    context: StoreContext,
    entity: Entity,
    sourceID: String,
    containerID: String,
    itemID: String,
    scheduleID: String,
    writable: Bool
) throws -> EKCalendarItem {
    _ = try context.container(identifier: containerID, entity: entity, sourceID: sourceID, writable: writable)
    guard let item = fetchItem(store: context.store, entity: entity, itemID: itemID) else {
        throw BridgeFailure("schedule_missing", "The EventKit item identifier no longer resolves.", details: ["item_id": itemID])
    }
    _ = try validateItemIdentity(item, entity: entity, sourceID: sourceID, containerID: containerID, scheduleID: scheduleID)
    return item
}

private func requireUniqueCurrentSchedule(
    context: StoreContext,
    entity: Entity,
    calendar: EKCalendar,
    currentItem: EKCalendarItem,
    scheduleID: String,
    searchWindow: DateWindow?,
    timeoutSeconds: Int
) throws -> EKCalendarItem {
    if entity == .event {
        guard let searchWindow, let event = currentItem as? EKEvent else {
            throw BridgeFailure("validation_error", "search_window is required for Calendar event mutation uniqueness checks.")
        }
        guard searchWindow.start <= event.startDate, searchWindow.end >= event.endDate else {
            throw BridgeFailure("validation_error", "search_window must contain the current Calendar event time before mutation.")
        }
    }
    guard let unique = try uniqueManagedMatch(
        context: context,
        entity: entity,
        calendar: calendar,
        scheduleID: scheduleID,
        searchWindow: searchWindow,
        timeoutSeconds: timeoutSeconds
    ) else {
        throw BridgeFailure("schedule_missing", "The managed item was not found by schedule_id during the mutation uniqueness check.")
    }
    guard itemIdentifier(unique) == itemIdentifier(currentItem) else {
        throw BridgeFailure("schedule_mismatch", "The item_id and schedule_id resolve to different EventKit items; mutation was refused.")
    }
    guard unique.refresh() else {
        throw BridgeFailure("schedule_missing", "The managed item changed or disappeared while preparing the mutation; retry from a fresh find/get preview.")
    }
    _ = try validateItemIdentity(
        unique,
        entity: entity,
        sourceID: calendar.source.sourceIdentifier,
        containerID: calendar.calendarIdentifier,
        scheduleID: scheduleID
    )
    return unique
}

private func commandDoctor() throws {
    let bundleID = Bundle.main.bundleIdentifier
    let plist = Bundle.main.infoDictionary ?? [:]
    let usageKeysPresent = [
        "calendar_full_access": !(plist["NSCalendarsFullAccessUsageDescription"] as? String ?? "").isEmpty,
        "reminders_full_access": !(plist["NSRemindersFullAccessUsageDescription"] as? String ?? "").isEmpty
    ]
    emitSuccess([
        "command": "doctor",
        "bundle": [
            "actual_id": bundleID ?? NSNull(),
            "expected_id": expectedBundleIdentifier,
            "identity_matches": bundleID == expectedBundleIdentifier,
            "usage_descriptions_present": usageKeysPresent,
            "signature_kind": plist["PersonalSchedulerSignatureKind"] as? String ?? "unknown",
            "tcc_identity_stable_across_rebuild": false,
            "ad_hoc_rebuild_may_require_reauthorization": true,
            "sandboxed": false,
            "bundle_path": Bundle.main.bundlePath
        ],
        "platform": [
            "os": "macOS",
            "version": ProcessInfo.processInfo.operatingSystemVersionString,
            "minimum_supported": "14.0"
        ],
        "event_store": [
            "events_permission": authorizationName(EKEventStore.authorizationStatus(for: .event)),
            "reminders_permission": authorizationName(EKEventStore.authorizationStatus(for: .reminder)),
            "data_accessed": false
        ],
        "capabilities": [
            "transport_protocol_version": protocolVersion,
            "managed_metadata_schema_version": managedSchemaVersion,
            "managed_marker": managedStartMarker,
            "verification_scope": "local_eventkit_readback",
            "icloud_delivery_verified": false,
            "managed_recurrence": false,
            "unmanaged_one_shot_mutation": true,
            "explicit_claim": true,
            "native_agenda_view": true,
            "operation_journal": "outer_personal_scheduler_executor",
            "reminder_relative_alarm_as_early_reminder": false,
            "uses_default_container": false,
            "container_delete": false,
            "optimistic_concurrency": "best_effort_fingerprint_plus_process_lock_not_atomic_cas"
        ],
        "mutated": false
    ])
}

private func commandSelfTest() throws {
    let input: [String: Any] = [
        "schema_version": managedSchemaVersion,
        "schedule_id": "PS-11111111-1111-4111-8111-111111111111",
        "entity": "reminder",
        "role": "task"
    ]
    let metadata = try ManagedMetadata(input: input, entity: .reminder)
    let merged = try mergeManagedBlock(existingNotes: "user note", metadata: metadata)
    guard let parsed = try parseManagedBlock(merged), parsed.metadata == metadata else {
        throw BridgeFailure("self_test_failed", "Managed marker round-trip failed.")
    }
    guard externalNotesPresent(merged, parsed: parsed) else {
        throw BridgeFailure("self_test_failed", "External note preservation failed.")
    }
    let firstHash = sha256(try jsonData(metadata.jsonObject))
    let secondHash = sha256(try jsonData(parsed.metadata.jsonObject))
    guard firstHash == secondHash else {
        throw BridgeFailure("self_test_failed", "Canonical hashing is unstable.")
    }
    for role in ["task", "deadline"] {
        var candidate = input
        candidate["role"] = role
        _ = try ManagedMetadata(input: candidate, entity: .reminder)
    }
    for role in ["appointment", "commitment", "time-block"] {
        var candidate = input
        candidate["entity"] = "event"
        candidate["role"] = role
        _ = try ManagedMetadata(input: candidate, entity: .event)
    }
    let allDay = try EventTiming(input: [
        "kind": "all_day",
        "start_date": "2026-09-07",
        "end_date_exclusive": "2026-09-08"
    ])
    guard allDay.jsonObject["timezone"] == nil else {
        throw BridgeFailure("self_test_failed", "All-day payloads must remain floating date boundaries.")
    }
    var rejectedOldMarker = false
    do {
        _ = try parseManagedBlock("[personal-scheduler:v0]\nschedule_id=PS-11111111-1111-4111-8111-111111111111\nentity=reminder\nrole=task\n[/personal-scheduler]")
    } catch let failure as BridgeFailure where failure.code == "unsupported_schema" {
        rejectedOldMarker = true
    }
    guard rejectedOldMarker else {
        throw BridgeFailure("self_test_failed", "The obsolete managed marker version was not rejected.")
    }
    guard ownershipInfo(nil).classification == "unmanaged",
          ownershipInfo("[goal-planner:v2]\ngoal_id=G-2026-001\n[/goal-planner]").classification == "goal_planner",
          ownershipInfo("[another-agent:v1]\nid=x\n[/another-agent]").classification == "foreign_marker",
          ownershipInfo("[OtherAgent:v1]\nid=x\n[/OtherAgent]").classification == "foreign_marker",
          ownershipInfo("[other_agent:v1]\nid=x\n[/other_agent]").classification == "foreign_marker",
          ownershipInfo("[other.agent:v1]\nid=x\n[/other.agent]").classification == "foreign_marker",
          ownershipInfo("[personal-scheduler:v1]\nschedule_id=PS-11111111-1111-4111-8111-111111111111\nentity=reminder\nrole=task\n[/personal-scheduler]\n[OtherAgent:v1]\n[/OtherAgent]").classification == "malformed_marker",
          ownershipInfo("[personal-scheduler:v1]\nbroken\n[/personal-scheduler]").classification == "malformed_marker" else {
        throw BridgeFailure("self_test_failed", "Ownership classification failed.")
    }
    _ = try parseDateTime("2026-09-01T14:00:00+08:00", field: "self_test")
    _ = try parseDateOnly("2026-09-01", field: "self_test")
    let reminderPayload = try ReminderPayload(input: [
        "title": "hash vector reminder",
        "due": ["kind": "date_time", "at": "2026-09-08T20:00:00+08:00", "timezone": "Asia/Shanghai"],
        "priority": 5
    ], allowUserNotes: false)
    let reminderContentHash = try canonicalFingerprint([
        "title": reminderPayload.title,
        "due": reminderPayload.due.jsonObject,
        "priority": reminderPayload.priority
    ])
    let eventPayload = try EventPayload(input: [
        "title": "hash vector event",
        "location": "Room 101",
        "time": ["kind": "timed", "start_at": "2026-09-08T14:00:00+08:00", "end_at": "2026-09-08T15:00:00+08:00", "timezone": "Asia/Shanghai"],
        "alarms": [["minutes_before": 30], ["minutes_before": 10]]
    ], allowUserNotes: false)
    let eventContentHash = try canonicalFingerprint([
        "title": eventPayload.title,
        "location": eventPayload.location ?? NSNull(),
        "time": eventPayload.timing.jsonObject,
        "alarms": eventPayload.alarmMinutesBefore.sorted().map { ["minutes_before": $0] }
    ])
    emitSuccess([
        "command": "self-test",
        "tests": [
            ["name": "managed_marker_round_trip", "passed": true],
            ["name": "external_notes_preserved", "passed": true],
            ["name": "canonical_hash_stable", "passed": true],
            ["name": "managed_v1_roles", "passed": true],
            ["name": "ownership_classification", "passed": true],
            ["name": "all_day_floating_dates", "passed": true],
            ["name": "obsolete_marker_rejected", "passed": true],
            ["name": "date_parsers", "passed": true]
        ],
        "content_hash_vectors": [
            "reminder": reminderContentHash,
            "event": eventContentHash
        ],
        "eventkit_data_accessed": false,
        "production_state_accessed": false,
        "mutated": false
    ])
}

private func commandAuthorize(_ input: [String: Any]) throws {
    try ensureAllowedKeys(input, ["entity", "confirmed", "timeout_seconds"], context: "request")
    let entity = try Entity.parse(try requiredString(input, "entity"))
    guard try requiredBool(input, "confirmed") else {
        throw BridgeFailure("confirmation_required", "Requesting macOS Calendar or Reminders permission requires explicit user confirmation.")
    }
    let timeoutSeconds = try optionalInt(input, "timeout_seconds", default: 120, range: 30...300)
    let initial = EKEventStore.authorizationStatus(for: entity.eventKitType)
    if initial.rawValue == 3 {
        emitSuccess([
            "command": "authorize",
            "entity": entity.rawValue,
            "status": "full_access",
            "prompted": false,
            "mutated": false
        ])
        return
    }
    if initial.rawValue == 1 {
        throw BridgeFailure("permission_restricted", "macOS restricts this permission; the bridge did not prompt again.")
    }
    if initial.rawValue == 2 {
        throw BridgeFailure("permission_denied", "This permission was previously denied; the bridge did not prompt again.")
    }
    guard #available(macOS 14.0, *) else {
        throw BridgeFailure("unsupported_platform", "Full EventKit access requires macOS 14 or later.")
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
    case .event:
        store.requestFullAccessToEvents(completion: completion)
    case .reminder:
        store.requestFullAccessToReminders(completion: completion)
    }
    if semaphore.wait(timeout: .now() + .seconds(timeoutSeconds)) == .timedOut {
        throw BridgeFailure("timeout", "The macOS permission request did not finish before the timeout.", details: ["timeout_seconds": timeoutSeconds])
    }
    lock.lock()
    let finalGranted = granted
    let finalError = requestError
    lock.unlock()
    if let finalError {
        throw eventKitFailure("permission_request_failed", "permission request", finalError)
    }
    let final = EKEventStore.authorizationStatus(for: entity.eventKitType)
    guard finalGranted, final.rawValue == 3 else {
        throw BridgeFailure("permission_denied", "Full EventKit access was not granted.", details: ["status": authorizationName(final)])
    }
    emitSuccess([
        "command": "authorize",
        "entity": entity.rawValue,
        "status": authorizationName(final),
        "prompted": true,
        "permission_state_changed": initial.rawValue != final.rawValue,
        "mutated": initial.rawValue != final.rawValue
    ])
}

private func commandSourcesList(_ input: [String: Any]) throws {
    try ensureAllowedKeys(input, ["entity"], context: "request")
    let entity = try Entity.parse(try requiredString(input, "entity"))
    let context = StoreContext()
    try context.requireFullAccess(entity)
    context.store.refreshSourcesIfNecessary()
    let sources = context.store.sources
        .filter { !$0.calendars(for: entity.eventKitType).isEmpty }
        .map { sourceJSON($0, entity: entity) }
        .sorted { ($0["title"] as? String ?? "").localizedCaseInsensitiveCompare($1["title"] as? String ?? "") == .orderedAscending }
    emitSuccess([
        "command": "sources list",
        "entity": entity.rawValue,
        "event_store_id": context.store.eventStoreIdentifier,
        "sources": sources,
        "requires_user_confirmation_of_icloud_source": true,
        "mutated": false
    ])
}

private func commandContainersList(_ input: [String: Any]) throws {
    try ensureAllowedKeys(input, ["entity", "source_id"], context: "request")
    let entity = try Entity.parse(try requiredString(input, "entity"))
    let sourceID = try optionalString(input, "source_id")
    let context = StoreContext()
    try context.requireFullAccess(entity)
    if let sourceID { _ = try context.source(identifier: sourceID, requireCalDAV: false) }
    let calendars = context.store.calendars(for: entity.eventKitType)
        .filter { sourceID == nil || $0.source.sourceIdentifier == sourceID }
        .map(containerJSON)
        .sorted {
            let left = ($0["source_title"] as? String ?? "") + "\u{0}" + ($0["title"] as? String ?? "")
            let right = ($1["source_title"] as? String ?? "") + "\u{0}" + ($1["title"] as? String ?? "")
            return left.localizedCaseInsensitiveCompare(right) == .orderedAscending
        }
    emitSuccess([
        "command": "containers list",
        "entity": entity.rawValue,
        "event_store_id": context.store.eventStoreIdentifier,
        "containers": calendars,
        "mutated": false
    ])
}

private func commandContainersCreate(_ input: [String: Any]) throws {
    try ensureAllowedKeys(input, ["entity", "source_id", "title", "confirm_icloud_source", "dry_run", "expected_event_store_id"], context: "request")
    let entity = try Entity.parse(try requiredString(input, "entity"))
    let sourceID = try requiredString(input, "source_id")
    let title = try requiredString(input, "title").trimmingCharacters(in: .whitespacesAndNewlines)
    let confirmed = try requiredBool(input, "confirm_icloud_source")
    let dryRun = try requiredBool(input, "dry_run")
    let expectedStoreID = try mutationExpectedStoreID(input, dryRun: dryRun)
    guard confirmed else {
        throw BridgeFailure("confirmation_required", "The caller must confirm that source_id is the intended iCloud source.")
    }
    guard !title.isEmpty, title.count <= 100 else {
        throw BridgeFailure("validation_error", "title must contain 1–100 characters.")
    }
    try rejectLineBreaks(title, field: "title")
    let context = StoreContext()
    try requireExpectedStoreID(expectedStoreID, context: context)
    try context.requireFullAccess(entity)
    let source = try context.source(identifier: sourceID, requireCalDAV: true)
    guard !source.isDelegate else {
        throw BridgeFailure("container_risk", "A delegated source cannot be used to create a personal-scheduler write container.")
    }
    let existing = context.store.calendars(for: entity.eventKitType).filter {
        $0.source.sourceIdentifier == sourceID && $0.title == title
    }
    if existing.count > 1 {
        throw BridgeFailure("container_ambiguous", "Multiple exact-title containers already exist in the confirmed source.", details: ["containers": existing.map(containerJSON)])
    }
    if let calendar = existing.first {
        throw BridgeFailure("container_exists", "An exact-title container already exists; reuse it by stable identifier instead of creating another.", details: ["container": containerJSON(calendar)])
    }
    if dryRun {
        emitSuccess([
            "command": "containers create",
            "dry_run": true,
            "event_store_id": context.store.eventStoreIdentifier,
            "would_create": ["entity": entity.rawValue, "source": sourceJSON(source), "title": title],
            "mutated": false
        ])
        return
    }
    try requireExpectedStoreID(expectedStoreID, context: context)
    let calendar = EKCalendar(for: entity.eventKitType, eventStore: context.store)
    calendar.title = title
    calendar.source = source
    do {
        try context.store.saveCalendar(calendar, commit: true)
    } catch {
        throw eventKitFailure("eventkit_write_failed", "container create", error)
    }
    let createdID = calendar.calendarIdentifier
    context.store.reset()
    let exactTitleContainers = context.store.calendars(for: entity.eventKitType).filter {
        $0.source.sourceIdentifier == sourceID && $0.title == title
    }
    guard exactTitleContainers.count == 1,
          let readback = context.store.calendar(withIdentifier: createdID),
          readback.source.sourceIdentifier == sourceID,
          readback.title == title,
          readback.allowsContentModifications,
          !readback.isSubscribed,
          !readback.isImmutable,
          !readback.source.isDelegate,
          readback.allowedEntityTypes.contains(entity == .event ? .event : .reminder) else {
        throw BridgeFailure("write_unverified", "The container was saved but could not be verified by EventKit readback.", details: ["container_id": createdID])
    }
    emitSuccess([
        "command": "containers create",
        "event_store_id": context.store.eventStoreIdentifier,
        "container": containerJSON(readback),
        "verification": "local_eventkit_readback",
        "icloud_delivery_verified": false,
        "mutated": true
    ])
}

private func availabilityName(_ value: EKEventAvailability) -> String {
    switch value {
    case .busy: return "busy"
    case .free: return "free"
    case .tentative: return "tentative"
    case .unavailable: return "unavailable"
    case .notSupported: return "not_supported"
    @unknown default: return "not_supported"
    }
}

private func commandAvailability(_ input: [String: Any]) throws {
    try ensureAllowedKeys(input, ["start_at", "end_at", "calendar_ids"], context: "request")
    let window = try DateWindow(input: [
        "start_at": try requiredString(input, "start_at"),
        "end_at": try requiredString(input, "end_at")
    ], context: "request", maximumDays: 62)
    let calendarIDs = try stringArray(input, "calendar_ids")
    guard !calendarIDs.isEmpty, calendarIDs.count <= 50 else {
        throw BridgeFailure("validation_error", "calendar_ids must contain 1–50 explicitly approved calendars.")
    }
    let context = StoreContext()
    try context.requireFullAccess(.event)
    var calendars: [EKCalendar] = []
    for identifier in calendarIDs {
        guard let calendar = context.store.calendar(withIdentifier: identifier), calendar.allowedEntityTypes.contains(.event) else {
            throw BridgeFailure("container_missing", "An approved availability calendar is unavailable or does not contain events.", details: ["container_id": identifier])
        }
        calendars.append(calendar)
    }
    let predicate = context.store.predicateForEvents(withStart: window.start, end: window.end, calendars: calendars)
    let intervals: [[String: Any]] = context.store.events(matching: predicate)
        .filter { $0.status != .canceled && $0.availability != .free }
        .map { event in
            [
                "start_at": isoString(event.startDate),
                "end_at": isoString(event.endDate),
                "all_day": event.isAllDay,
                "calendar_id": event.calendar.calendarIdentifier,
                "availability": availabilityName(event.availability),
                "busy_strength": event.availability == .tentative ? "soft" : "hard"
            ]
        }
        .sorted {
            ($0["start_at"] as? String ?? "") < ($1["start_at"] as? String ?? "")
        }
    emitSuccess([
        "command": "availability",
        "window": window.jsonObject,
        "event_store_id": context.store.eventStoreIdentifier,
        "calendar_ids": calendarIDs,
        "intervals": intervals,
        "privacy_scope": "occupied_intervals_only",
        "mutated": false
    ])
}

private func commandEventsList(_ input: [String: Any]) throws {
    try ensureAllowedKeys(input, ["start_at", "end_at", "calendar_ids", "detail"], context: "request")
    let window = try DateWindow(input: [
        "start_at": try requiredString(input, "start_at"),
        "end_at": try requiredString(input, "end_at")
    ], context: "request", maximumDays: 62)
    let calendarIDs = try stringArray(input, "calendar_ids")
    guard !calendarIDs.isEmpty, calendarIDs.count <= 50 else {
        throw BridgeFailure("validation_error", "calendar_ids must contain 1–50 explicitly approved calendars.")
    }
    let detail = try requiredString(input, "detail")
    guard detail == "busy" || detail == "summary" else {
        throw BridgeFailure("validation_error", "detail must be 'busy' or 'summary'.")
    }
    let context = StoreContext()
    try context.requireFullAccess(.event)
    let calendars = try calendarIDs.map { identifier -> EKCalendar in
        guard let calendar = context.store.calendar(withIdentifier: identifier), calendar.allowedEntityTypes.contains(.event) else {
            throw BridgeFailure("container_missing", "An approved Calendar is unavailable or does not contain events.", details: ["container_id": identifier])
        }
        return calendar
    }
    let predicate = context.store.predicateForEvents(withStart: window.start, end: window.end, calendars: calendars)
    let items = try context.store.events(matching: predicate).map { event -> [String: Any] in
        let snapshot = try safeSnapshot(event, entity: .event)
        var item: [String: Any] = [
            "entity": "event",
            "item_id": snapshot["item_id"]!,
            "container_id": snapshot["container_id"]!,
            "source_id": snapshot["source_id"]!,
            "ownership": snapshot["ownership"]!,
            "all_day": snapshot["all_day"]!,
            "availability": availabilityName(event.availability),
            "status": event.status.rawValue,
            "recurring": snapshot["recurring"]!,
            "fingerprint": snapshot["fingerprint"]!
        ]
        if event.isAllDay {
            item["start_date"] = snapshot["start_date"]
            item["end_date_exclusive"] = snapshot["end_date_exclusive"]
        } else {
            item["start_at"] = snapshot["start_at"]
            item["end_at"] = snapshot["end_at"]
            item["timezone"] = snapshot["timezone"]
        }
        if let managed = snapshot["managed"] { item["managed"] = managed }
        if detail == "summary" {
            item["title"] = event.title ?? ""
            item["location"] = event.location ?? NSNull()
            item["has_notes"] = snapshot["has_notes"]
        }
        return item
    }.sorted {
        let left = ($0["start_at"] as? String) ?? ($0["start_date"] as? String) ?? ""
        let right = ($1["start_at"] as? String) ?? ($1["start_date"] as? String) ?? ""
        return left < right
    }
    emitSuccess([
        "command": "events list",
        "detail": detail,
        "window": window.jsonObject,
        "event_store_id": context.store.eventStoreIdentifier,
        "calendar_ids": calendarIDs,
        "items": items,
        "mutated": false
    ])
}

private func reminderDueDate(_ reminder: EKReminder) -> Date? {
    guard let components = reminder.dueDateComponents else { return nil }
    var calendar = components.calendar ?? Calendar(identifier: .gregorian)
    calendar.timeZone = components.timeZone ?? reminder.timeZone ?? TimeZone.current
    return calendar.date(from: components)
}

private func commandRemindersList(_ input: [String: Any]) throws {
    try ensureAllowedKeys(input, ["list_ids", "due_window", "include_overdue", "include_undated", "include_completed", "timeout_seconds", "limit"], context: "request")
    let listIDs = try stringArray(input, "list_ids")
    guard !listIDs.isEmpty, listIDs.count <= 50 else {
        throw BridgeFailure("validation_error", "list_ids must contain 1–50 explicitly approved Reminder lists.")
    }
    let dueWindow = try optionalObject(input, "due_window").map { try DateWindow(input: $0, context: "due_window", maximumDays: 366) }
    let includeOverdue = try optionalBool(input, "include_overdue", default: false)
    let includeUndated = try optionalBool(input, "include_undated", default: false)
    let includeCompleted = try optionalBool(input, "include_completed", default: false)
    let timeoutSeconds = try optionalInt(input, "timeout_seconds", default: 20, range: 5...60)
    let limit = try optionalInt(input, "limit", default: 200, range: 1...500)
    let context = StoreContext()
    try context.requireFullAccess(.reminder)
    let calendars = try listIDs.map { identifier in
        guard let calendar = context.store.calendar(withIdentifier: identifier), calendar.allowedEntityTypes.contains(.reminder) else {
            throw BridgeFailure("container_missing", "An approved Reminder list is unavailable.", details: ["container_id": identifier])
        }
        return calendar
    }
    let reminders = try fetchReminders(store: context.store, calendars: calendars, timeoutSeconds: timeoutSeconds)
    var items: [[String: Any]] = []
    for reminder in reminders {
        if reminder.isCompleted && !includeCompleted { continue }
        if let due = reminderDueDate(reminder) {
            if let dueWindow {
                let inWindow = due >= dueWindow.start && due < dueWindow.end
                let overdue = includeOverdue && due < dueWindow.start && !reminder.isCompleted
                if !inWindow && !overdue { continue }
            }
        } else if !includeUndated {
            continue
        }
        items.append(try safeSnapshot(reminder, entity: .reminder))
    }
    items.sort {
        let left = ($0["due_components"] as? [String: Any]).flatMap { try? jsonData($0) } ?? Data([0xFF])
        let right = ($1["due_components"] as? [String: Any]).flatMap { try? jsonData($0) } ?? Data([0xFF])
        return left.lexicographicallyPrecedes(right)
    }
    let truncated = items.count > limit
    if truncated { items = Array(items.prefix(limit)) }
    emitSuccess([
        "command": "reminders list",
        "event_store_id": context.store.eventStoreIdentifier,
        "list_ids": listIDs,
        "items": items,
        "truncated": truncated,
        "mutated": false
    ])
}

private func apply(_ payload: ReminderPayload, metadata: ManagedMetadata, to reminder: EKReminder, creating: Bool) throws {
    if creating, ownershipInfo(payload.userNotes).classification != "unmanaged" {
        throw BridgeFailure("ownership_conflict", "user_notes must not contain managed-marker syntax.")
    }
    reminder.title = payload.title
    reminder.notes = try mergeManagedBlock(existingNotes: creating ? payload.userNotes : reminder.notes, metadata: metadata)
    reminder.dueDateComponents = payload.due.dateComponents
    reminder.priority = payload.priority
    if creating {
        reminder.startDateComponents = nil
        reminder.isCompleted = false
        reminder.alarms = nil
        reminder.recurrenceRules = nil
    }
}

private func apply(_ payload: EventPayload, metadata: ManagedMetadata, to event: EKEvent, creating: Bool) throws {
    if creating, ownershipInfo(payload.userNotes).classification != "unmanaged" {
        throw BridgeFailure("ownership_conflict", "user_notes must not contain managed-marker syntax.")
    }
    let locationChanges = event.location != payload.location
    if !creating,
       locationChanges,
       let structured = event.structuredLocation,
       structured.geoLocation != nil || abs(structured.radius) > 0.000_001 {
        throw BridgeFailure(
            "unsupported_structured_location",
            "Changing this event's location would discard coordinates or radius managed by Calendar; patch was refused. Change it manually or remove the structured location first."
        )
    }
    event.title = payload.title
    if creating || locationChanges {
        event.location = payload.location
    }
    event.notes = try mergeManagedBlock(existingNotes: creating ? payload.userNotes : event.notes, metadata: metadata)
    event.startDate = payload.timing.start
    event.endDate = payload.timing.end
    event.isAllDay = payload.timing.allDay
    // EventKit models all-day dates as floating calendar-day boundaries in the
    // user's current default time zone. Never pin them to an arbitrary IANA zone.
    event.timeZone = payload.timing.timeZone
    event.alarms = payload.alarmMinutesBefore.map { minutes in
        EKAlarm(relativeOffset: -Double(minutes * 60))
    }
    if creating {
        event.recurrenceRules = nil
    }
}

private func reminderDueMatches(_ due: ReminderDue, _ components: DateComponents?) -> Bool {
    switch due {
    case .none:
        return components == nil
    case let .date(year, month, day):
        guard let components else { return false }
        return components.year == year
            && components.month == month
            && components.day == day
            && components.hour == nil
            && components.minute == nil
            && components.second == nil
    case let .dateTime(date, timeZone):
        guard let components else { return false }
        var calendar = components.calendar ?? Calendar(identifier: .gregorian)
        calendar.timeZone = components.timeZone ?? timeZone
        guard let actual = calendar.date(from: components) else { return false }
        let sameInstant = abs(actual.timeIntervalSince(date)) < 1.0
        let sameZone = components.timeZone?.identifier == timeZone.identifier
        return sameInstant && sameZone
    }
}

private func managedMetadata(_ item: EKCalendarItem) throws -> ManagedMetadata {
    guard let parsed = try parseManagedBlock(item.notes) else {
        throw BridgeFailure("readback_mismatch", "The managed metadata marker is missing after write.")
    }
    return parsed.metadata
}

private func verify(_ reminder: EKReminder, payload: ReminderPayload, metadata: ManagedMetadata, expectedNotes: String) throws {
    try assertNonRecurring(reminder)
    var mismatches: [String] = []
    if reminder.title != payload.title { mismatches.append("title") }
    if reminder.notes != expectedNotes { mismatches.append("notes") }
    if try managedMetadata(reminder) != metadata { mismatches.append("managed") }
    if !reminderDueMatches(payload.due, reminder.dueDateComponents) { mismatches.append("due") }
    if reminder.priority != payload.priority { mismatches.append("priority") }
    if reminder.hasAlarms || !(reminder.alarms?.isEmpty ?? true) { mismatches.append("alarms") }
    if !mismatches.isEmpty {
        throw BridgeFailure("readback_mismatch", "The Reminder readback differs from the requested schedule.", details: ["fields": mismatches, "actual": try safeSnapshot(reminder, entity: .reminder)])
    }
}

private func relativeAlarmMinutes(_ event: EKEvent) -> [Int]? {
    let alarms = event.alarms ?? []
    guard alarms.count <= 5 else { return nil }
    var minutes: [Int] = []
    for alarm in alarms {
        if alarm.type != .display
            || alarm.absoluteDate != nil
            || alarm.structuredLocation != nil
            || alarm.proximity != .none {
            return nil
        }
        let raw = -alarm.relativeOffset / 60.0
        guard raw >= 0,
              raw <= 10_080,
              abs(raw.rounded() - raw) < 0.000_001 else { return nil }
        minutes.append(Int(raw.rounded()))
    }
    guard Set(minutes).count == minutes.count else { return nil }
    return minutes.sorted()
}

private func verify(_ event: EKEvent, payload: EventPayload, metadata: ManagedMetadata, expectedNotes: String) throws {
    try assertNonRecurring(event)
    var mismatches: [String] = []
    if event.title != payload.title { mismatches.append("title") }
    if event.notes != expectedNotes { mismatches.append("notes") }
    if event.location != payload.location { mismatches.append("location") }
    if try managedMetadata(event) != metadata { mismatches.append("managed") }
    if event.isAllDay != payload.timing.allDay { mismatches.append("all_day") }
    switch payload.timing {
    case let .timed(_, _, timeZone):
        if abs(event.startDate.timeIntervalSince(payload.timing.start)) >= 1.0 { mismatches.append("start") }
        if abs(event.endDate.timeIntervalSince(payload.timing.end)) >= 1.0 { mismatches.append("end") }
        if event.timeZone?.identifier != timeZone.identifier { mismatches.append("timezone") }
    case let .allDay(_, _, startText, endText):
        let calendar = defaultAllDayCalendar()
        let actualStart = allDayDateString(event.startDate, calendar: calendar)
        let actualEnd = allDayDateString(event.endDate, calendar: calendar)
        if actualStart != startText { mismatches.append("start_date") }
        if actualEnd != endText { mismatches.append("end_date_exclusive") }
    }
    if relativeAlarmMinutes(event) != payload.alarmMinutesBefore.sorted() { mismatches.append("alarms") }
    if !mismatches.isEmpty {
        throw BridgeFailure("readback_mismatch", "The Calendar event readback differs from the requested schedule.", details: ["fields": mismatches, "actual": try safeSnapshot(event, entity: .event)])
    }
}

private func readbackManagedItem(
    context: StoreContext,
    entity: Entity,
    sourceID: String,
    containerID: String,
    itemID: String,
    scheduleID: String
) throws -> EKCalendarItem {
    context.store.reset()
    guard let item = fetchItem(store: context.store, entity: entity, itemID: itemID) else {
        throw BridgeFailure(
            "write_unverified",
            "EventKit accepted the write, but the item was not available by identifier during readback. Search by schedule_id before retrying create.",
            details: ["item_id": itemID, "schedule_id": scheduleID]
        )
    }
    _ = try validateItemIdentity(item, entity: entity, sourceID: sourceID, containerID: containerID, scheduleID: scheduleID)
    return item
}

private func saveReminder(_ reminder: EKReminder, store: EKEventStore, operation: String) throws {
    do {
        try store.save(reminder, commit: true)
    } catch {
        throw eventKitFailure("eventkit_write_failed", operation, error)
    }
}

private func saveEvent(_ event: EKEvent, store: EKEventStore, operation: String) throws {
    do {
        try store.save(event, span: .thisEvent, commit: true)
    } catch {
        throw eventKitFailure("eventkit_write_failed", operation, error)
    }
}

private func parseItemLocator(_ input: [String: Any], entity: Entity, writable: Bool) throws -> (StoreContext, String, String, String, String, EKCalendarItem) {
    let sourceID = try requiredString(input, "source_id")
    let containerID = try requiredString(input, "container_id")
    let itemID = try requiredString(input, "item_id")
    let scheduleID = try requiredString(input, "schedule_id")
    try validateScheduleIDShape(scheduleID)
    let context = StoreContext()
    try context.requireFullAccess(entity)
    let item = try loadManagedItem(
        context: context,
        entity: entity,
        sourceID: sourceID,
        containerID: containerID,
        itemID: itemID,
        scheduleID: scheduleID,
        writable: writable
    )
    return (context, sourceID, containerID, itemID, scheduleID, item)
}

private func loadExactItem(_ input: [String: Any], entity: Entity, writable: Bool) throws -> (StoreContext, String, String, String, EKCalendarItem) {
    let sourceID = try requiredString(input, "source_id")
    let containerID = try requiredString(input, "container_id")
    let itemID = try requiredString(input, "item_id")
    let context = StoreContext()
    try context.requireFullAccess(entity)
    _ = try context.container(identifier: containerID, entity: entity, sourceID: sourceID, writable: writable)
    guard let item = fetchItem(store: context.store, entity: entity, itemID: itemID) else {
        throw BridgeFailure("item_missing", "The EventKit item identifier no longer resolves.", details: ["item_id": itemID])
    }
    guard item.calendar.calendarIdentifier == containerID, item.calendar.source.sourceIdentifier == sourceID else {
        throw BridgeFailure("item_moved", "The item no longer belongs to the expected source and container.")
    }
    try assertNonRecurring(item)
    return (context, sourceID, containerID, itemID, item)
}

private func requireUnmanagedMutationEligibility(_ item: EKCalendarItem, entity: Entity) throws {
    let ownership = ownershipInfo(item.notes)
    guard ownership.classification == "unmanaged" else {
        throw BridgeFailure("ownership_conflict", "One-shot mutation and claim require an unmanaged item.", details: ["ownership": ownership.classification])
    }
    try assertNonRecurring(item)
    if item.hasAlarms || !(item.alarms?.isEmpty ?? true) {
        switch entity {
        case .reminder:
            throw BridgeFailure("unsupported_reminder_alarm", "One-shot mutation of a Reminder with alarms is fail-closed.")
        case .event:
            guard let event = item as? EKEvent, relativeAlarmMinutes(event) != nil else {
                throw BridgeFailure("unsupported_event_alarm", "The Calendar event contains an unsupported alarm.")
            }
        }
    }
    if let event = item as? EKEvent, eventHasInvitation(event) {
        throw BridgeFailure("unsupported_invitation", "Calendar events with attendees or an organizer cannot be claimed or mutated.")
    }
    if let event = item as? EKEvent, event.structuredLocation != nil {
        throw BridgeFailure("unsupported_structured_location", "Calendar events with a structured location cannot be claimed, patched, or deleted by a one-shot operation.")
    }
}

private func commandItemsFind(_ input: [String: Any]) throws {
    try ensureAllowedKeys(input, ["entity", "source_id", "container_id", "schedule_id", "search_window", "timeout_seconds"], context: "request")
    let entity = try Entity.parse(try requiredString(input, "entity"))
    let sourceID = try requiredString(input, "source_id")
    let containerID = try requiredString(input, "container_id")
    let scheduleID = try requiredString(input, "schedule_id")
    try validateScheduleIDShape(scheduleID)
    let timeoutSeconds = try optionalInt(input, "timeout_seconds", default: 20, range: 5...60)
    let searchWindow = try optionalObject(input, "search_window").map { try DateWindow(input: $0, context: "search_window", maximumDays: 366) }
    if entity == .event, searchWindow == nil {
        throw BridgeFailure("validation_error", "search_window is required when finding a Calendar event.")
    }
    let context = StoreContext()
    try context.requireFullAccess(entity)
    let calendar = try context.container(identifier: containerID, entity: entity, sourceID: sourceID, writable: false)
    let match = try uniqueManagedMatch(
        context: context,
        entity: entity,
        calendar: calendar,
        scheduleID: scheduleID,
        searchWindow: searchWindow,
        timeoutSeconds: timeoutSeconds
    )
    emitSuccess([
        "command": "items find",
        "entity": entity.rawValue,
        "schedule_id": scheduleID,
        "count": match == nil ? 0 : 1,
        "event_store_id": context.store.eventStoreIdentifier,
        "item": try match.map { try safeSnapshot($0, entity: entity) } ?? NSNull(),
        "mutated": false
    ])
}

private func commandItemsGet(_ input: [String: Any]) throws {
    try ensureAllowedKeys(input, ["entity", "source_id", "container_id", "item_id", "expected_schedule_id"], context: "request")
    let entity = try Entity.parse(try requiredString(input, "entity"))
    let sourceID = try requiredString(input, "source_id")
    let containerID = try requiredString(input, "container_id")
    let itemID = try requiredString(input, "item_id")
    let expectedScheduleID = try optionalString(input, "expected_schedule_id")
    let context = StoreContext()
    try context.requireFullAccess(entity)
    _ = try context.container(identifier: containerID, entity: entity, sourceID: sourceID, writable: false)
    guard let item = fetchItem(store: context.store, entity: entity, itemID: itemID) else {
        throw BridgeFailure("item_missing", "The EventKit item identifier no longer resolves.", details: ["item_id": itemID])
    }
    guard item.calendar.calendarIdentifier == containerID, item.calendar.source.sourceIdentifier == sourceID else {
        throw BridgeFailure("item_moved", "The item no longer belongs to the expected source and container.")
    }
    if let expectedScheduleID {
        try validateScheduleIDShape(expectedScheduleID)
        _ = try validateItemIdentity(item, entity: entity, sourceID: sourceID, containerID: containerID, scheduleID: expectedScheduleID)
    }
    var output: [String: Any] = [
        "command": "items get",
        "entity": entity.rawValue,
        "event_store_id": context.store.eventStoreIdentifier,
        "item": try safeSnapshot(item, entity: entity),
        "mutated": false
    ]
    if let expectedScheduleID { output["expected_schedule_id"] = expectedScheduleID }
    emitSuccess(output)
}

private func commandItemsCreate(_ input: [String: Any]) throws {
    try ensureAllowedKeys(input, ["entity", "source_id", "container_id", "managed", "payload", "search_window", "timeout_seconds", "dry_run", "confirm_private_container", "expected_event_store_id"], context: "request")
    try requirePrivateWriteConfirmation(input)
    let entity = try Entity.parse(try requiredString(input, "entity"))
    let sourceID = try requiredString(input, "source_id")
    let containerID = try requiredString(input, "container_id")
    let metadata = try ManagedMetadata(input: try requiredObject(input, "managed"), entity: entity)
    let timeoutSeconds = try optionalInt(input, "timeout_seconds", default: 20, range: 5...60)
    let dryRun = try requiredBool(input, "dry_run")
    let expectedStoreID = try mutationExpectedStoreID(input, dryRun: dryRun)
    let searchWindow = try optionalObject(input, "search_window").map { try DateWindow(input: $0, context: "search_window", maximumDays: 366) }
    let reminderPayload: ReminderPayload?
    let eventPayload: EventPayload?
    switch entity {
    case .reminder:
        reminderPayload = try ReminderPayload(input: try requiredObject(input, "payload"), allowUserNotes: true)
        eventPayload = nil
    case .event:
        let payload = try EventPayload(input: try requiredObject(input, "payload"), allowUserNotes: true)
        guard let searchWindow else {
            throw BridgeFailure("validation_error", "search_window is required for Calendar event create duplicate protection.")
        }
        guard searchWindow.start <= payload.timing.start, searchWindow.end >= payload.timing.end else {
            throw BridgeFailure("validation_error", "search_window must contain the requested event time.")
        }
        eventPayload = payload
        reminderPayload = nil
    }

    let context = StoreContext()
    try requireExpectedStoreID(expectedStoreID, context: context)
    try context.requireFullAccess(entity)
    let calendar = try context.container(identifier: containerID, entity: entity, sourceID: sourceID, writable: true)

    if let existing = try uniqueManagedMatch(
        context: context,
        entity: entity,
        calendar: calendar,
        scheduleID: metadata.scheduleID,
        searchWindow: searchWindow,
        timeoutSeconds: timeoutSeconds
    ) {
        throw BridgeFailure("schedule_exists", "A managed item already exists for this schedule_id; create was refused.", details: ["item": try safeSnapshot(existing, entity: entity)])
    }

    if dryRun {
        emitSuccess([
            "command": "items create",
            "dry_run": true,
            "entity": entity.rawValue,
            "event_store_id": context.store.eventStoreIdentifier,
            "source_id": sourceID,
            "container_id": containerID,
            "managed": metadata.jsonObject,
            "desired": reminderPayload?.safeJSON ?? eventPayload!.safeJSON,
            "mutated": false
        ])
        return
    }

    try requireExpectedStoreID(expectedStoreID, context: context)
    let expectedNotes: String
    switch entity {
    case .reminder:
        expectedNotes = try mergeManagedBlock(existingNotes: reminderPayload!.userNotes, metadata: metadata)
    case .event:
        expectedNotes = try mergeManagedBlock(existingNotes: eventPayload!.userNotes, metadata: metadata)
    }
    let createdID: String
    switch entity {
    case .reminder:
        let reminder = EKReminder(eventStore: context.store)
        reminder.calendar = calendar
        try apply(reminderPayload!, metadata: metadata, to: reminder, creating: true)
        try saveReminder(reminder, store: context.store, operation: "reminder create")
        createdID = reminder.calendarItemIdentifier
    case .event:
        let event = EKEvent(eventStore: context.store)
        event.calendar = calendar
        try apply(eventPayload!, metadata: metadata, to: event, creating: true)
        try saveEvent(event, store: context.store, operation: "event create")
        createdID = event.eventIdentifier ?? event.calendarItemIdentifier
    }
    let item = try readbackManagedItem(
        context: context,
        entity: entity,
        sourceID: sourceID,
        containerID: containerID,
        itemID: createdID,
        scheduleID: metadata.scheduleID
    )
    let readbackCalendar = try context.container(identifier: containerID, entity: entity, sourceID: sourceID, writable: true)
    let verifiedItem = try requireUniqueCurrentSchedule(
        context: context,
        entity: entity,
        calendar: readbackCalendar,
        currentItem: item,
        scheduleID: metadata.scheduleID,
        searchWindow: searchWindow,
        timeoutSeconds: timeoutSeconds
    )
    switch entity {
    case .reminder: try verify(verifiedItem as! EKReminder, payload: reminderPayload!, metadata: metadata, expectedNotes: expectedNotes)
    case .event: try verify(verifiedItem as! EKEvent, payload: eventPayload!, metadata: metadata, expectedNotes: expectedNotes)
    }
    emitSuccess([
        "command": "items create",
        "entity": entity.rawValue,
        "event_store_id": context.store.eventStoreIdentifier,
        "item": try safeSnapshot(verifiedItem, entity: entity),
        "verification": "local_eventkit_readback",
        "icloud_delivery_verified": false,
        "mutated": true
    ])
}

private func commandItemsPatch(_ input: [String: Any]) throws {
    try ensureAllowedKeys(input, ["entity", "source_id", "container_id", "schedule_id", "item_id", "expected_fingerprint", "managed", "payload", "search_window", "timeout_seconds", "dry_run", "confirm_private_container", "expected_event_store_id"], context: "request")
    try requirePrivateWriteConfirmation(input)
    let entity = try Entity.parse(try requiredString(input, "entity"))
    let expectedFingerprint = try requiredString(input, "expected_fingerprint")
    let metadata = try ManagedMetadata(input: try requiredObject(input, "managed"), entity: entity)
    let timeoutSeconds = try optionalInt(input, "timeout_seconds", default: 20, range: 5...60)
    let dryRun = try requiredBool(input, "dry_run")
    let expectedStoreID = try mutationExpectedStoreID(input, dryRun: dryRun)
    let searchWindow = try optionalObject(input, "search_window").map { try DateWindow(input: $0, context: "search_window", maximumDays: 366) }
    if entity == .event, searchWindow == nil {
        throw BridgeFailure("validation_error", "search_window is required for Calendar event patch.")
    }
    let reminderPayload: ReminderPayload?
    let eventPayload: EventPayload?
    switch entity {
    case .reminder:
        reminderPayload = try ReminderPayload(input: try requiredObject(input, "payload"), allowUserNotes: false)
        eventPayload = nil
    case .event:
        reminderPayload = nil
        eventPayload = try EventPayload(input: try requiredObject(input, "payload"), allowUserNotes: false)
    }
    if entity == .event {
        guard let searchWindow, let payload = eventPayload else {
            throw BridgeFailure("validation_error", "search_window and an event payload are required for Calendar event patch.")
        }
        guard searchWindow.start <= payload.timing.start, searchWindow.end >= payload.timing.end else {
            throw BridgeFailure("validation_error", "search_window must contain the desired Calendar event time before patch.")
        }
    }
    let (context, sourceID, containerID, itemID, scheduleID, locatorItem) = try parseItemLocator(input, entity: entity, writable: true)
    try requireExpectedStoreID(expectedStoreID, context: context)
    guard metadata.scheduleID == scheduleID else {
        throw BridgeFailure("validation_error", "managed.schedule_id must match request.schedule_id.")
    }
    let calendar = try context.container(identifier: containerID, entity: entity, sourceID: sourceID, writable: true)
    let item = try requireUniqueCurrentSchedule(
        context: context,
        entity: entity,
        calendar: calendar,
        currentItem: locatorItem,
        scheduleID: scheduleID,
        searchWindow: searchWindow,
        timeoutSeconds: timeoutSeconds
    )
    let existingMetadata = try managedMetadata(item)
    guard existingMetadata == metadata else {
        throw BridgeFailure("identity_change_forbidden", "patch cannot change schedule identity, entity, or role.")
    }
    let current = try requireExpectedFingerprint(item, entity: entity, expected: expectedFingerprint)

    switch entity {
    case .reminder:
        if item.hasAlarms || !(item.alarms?.isEmpty ?? true) {
            throw BridgeFailure("unsupported_reminder_alarm", "This Reminder has an alarm outside the managed v1 contract; patch was refused so alert semantics are not changed.", details: ["current": current])
        }
    case .event:
        let event = item as! EKEvent
        if eventHasInvitation(event) {
            throw BridgeFailure("unsupported_invitation", "Calendar events with attendees or an organizer are not managed; patch was refused to avoid invitation updates.", details: ["current": current])
        }
        if relativeAlarmMinutes(event) == nil {
            throw BridgeFailure("unsupported_event_alarm", "This Calendar event contains an alarm type or trigger outside the bridge contract; patch was refused so alarm semantics are not changed.", details: ["current": current])
        }
        if event.structuredLocation != nil {
            throw BridgeFailure("unsupported_structured_location", "Patch was refused because the event contains a structured location outside the managed v1 contract.", details: ["current": current])
        }
    }

    if dryRun {
        emitSuccess([
            "command": "items patch",
            "dry_run": true,
            "entity": entity.rawValue,
            "event_store_id": context.store.eventStoreIdentifier,
            "current": current,
            "managed": metadata.jsonObject,
            "desired": reminderPayload?.safeJSON ?? eventPayload!.safeJSON,
            "mutated": false
        ])
        return
    }

    try requireExpectedStoreID(expectedStoreID, context: context)
    let expectedNotes = try mergeManagedBlock(existingNotes: item.notes, metadata: metadata)
    switch entity {
    case .reminder:
        let reminder = item as! EKReminder
        try apply(reminderPayload!, metadata: metadata, to: reminder, creating: false)
        try saveReminder(reminder, store: context.store, operation: "reminder patch")
    case .event:
        let event = item as! EKEvent
        try apply(eventPayload!, metadata: metadata, to: event, creating: false)
        try saveEvent(event, store: context.store, operation: "event patch")
    }

    let readback = try readbackManagedItem(
        context: context,
        entity: entity,
        sourceID: sourceID,
        containerID: containerID,
        itemID: itemID,
        scheduleID: scheduleID
    )
    let readbackCalendar = try context.container(identifier: containerID, entity: entity, sourceID: sourceID, writable: true)
    let verifiedReadback = try requireUniqueCurrentSchedule(
        context: context,
        entity: entity,
        calendar: readbackCalendar,
        currentItem: readback,
        scheduleID: scheduleID,
        searchWindow: searchWindow,
        timeoutSeconds: timeoutSeconds
    )
    switch entity {
    case .reminder: try verify(verifiedReadback as! EKReminder, payload: reminderPayload!, metadata: metadata, expectedNotes: expectedNotes)
    case .event: try verify(verifiedReadback as! EKEvent, payload: eventPayload!, metadata: metadata, expectedNotes: expectedNotes)
    }
    emitSuccess([
        "command": "items patch",
        "entity": entity.rawValue,
        "event_store_id": context.store.eventStoreIdentifier,
        "item": try safeSnapshot(verifiedReadback, entity: entity),
        "verification": "local_eventkit_readback",
        "icloud_delivery_verified": false,
        "mutated": true
    ])
}

private func commandRemindersComplete(_ input: [String: Any]) throws {
    try ensureAllowedKeys(input, ["source_id", "container_id", "schedule_id", "item_id", "expected_fingerprint", "timeout_seconds", "dry_run", "confirm_private_container", "expected_event_store_id"], context: "request")
    try requirePrivateWriteConfirmation(input)
    let expectedFingerprint = try requiredString(input, "expected_fingerprint")
    let timeoutSeconds = try optionalInt(input, "timeout_seconds", default: 20, range: 5...60)
    let dryRun = try requiredBool(input, "dry_run")
    let expectedStoreID = try mutationExpectedStoreID(input, dryRun: dryRun)
    let (context, sourceID, containerID, itemID, scheduleID, locatorItem) = try parseItemLocator(input, entity: .reminder, writable: true)
    try requireExpectedStoreID(expectedStoreID, context: context)
    let calendar = try context.container(identifier: containerID, entity: .reminder, sourceID: sourceID, writable: true)
    let item = try requireUniqueCurrentSchedule(
        context: context,
        entity: .reminder,
        calendar: calendar,
        currentItem: locatorItem,
        scheduleID: scheduleID,
        searchWindow: nil,
        timeoutSeconds: timeoutSeconds
    )
    let current = try requireExpectedFingerprint(item, entity: .reminder, expected: expectedFingerprint)
    let reminder = item as! EKReminder
    if reminder.hasAlarms || !(reminder.alarms?.isEmpty ?? true) {
        throw BridgeFailure("unsupported_reminder_alarm", "Completion was refused because the Reminder contains alarms outside the managed contract.", details: ["current": current])
    }
    if reminder.isCompleted {
        emitSuccess([
            "command": "reminders complete",
            "event_store_id": context.store.eventStoreIdentifier,
            "item": current,
            "already_completed": true,
            "verification": "local_eventkit_readback",
            "icloud_delivery_verified": false,
            "mutated": false
        ])
        return
    }
    if dryRun {
        emitSuccess([
            "command": "reminders complete",
            "dry_run": true,
            "event_store_id": context.store.eventStoreIdentifier,
            "current": current,
            "would_set_completed": true,
            "mutated": false
        ])
        return
    }
    let expectedNotes = reminder.notes
    try requireExpectedStoreID(expectedStoreID, context: context)
    reminder.isCompleted = true
    try saveReminder(reminder, store: context.store, operation: "reminder complete")
    let readback = try readbackManagedItem(
        context: context,
        entity: .reminder,
        sourceID: sourceID,
        containerID: containerID,
        itemID: itemID,
        scheduleID: scheduleID
    )
    guard let verified = readback as? EKReminder, verified.isCompleted, verified.notes == expectedNotes else {
        throw BridgeFailure("readback_mismatch", "The Reminder was saved but completion was not confirmed by readback.")
    }
    let readbackCalendar = try context.container(identifier: containerID, entity: .reminder, sourceID: sourceID, writable: true)
    let verifiedItem = try requireUniqueCurrentSchedule(
        context: context,
        entity: .reminder,
        calendar: readbackCalendar,
        currentItem: verified,
        scheduleID: scheduleID,
        searchWindow: nil,
        timeoutSeconds: timeoutSeconds
    )
    guard let finalReminder = verifiedItem as? EKReminder, finalReminder.isCompleted, finalReminder.notes == expectedNotes else {
        throw BridgeFailure("readback_mismatch", "The final Reminder uniqueness readback did not preserve completion and notes.", details: ["fields": ["completed", "notes"]])
    }
    emitSuccess([
        "command": "reminders complete",
        "event_store_id": context.store.eventStoreIdentifier,
        "item": try safeSnapshot(verifiedItem, entity: .reminder),
        "verification": "local_eventkit_readback",
        "icloud_delivery_verified": false,
        "mutated": true
    ])
}

private func commandItemsDelete(_ input: [String: Any]) throws {
    try ensureAllowedKeys(input, ["entity", "source_id", "container_id", "schedule_id", "item_id", "expected_fingerprint", "search_window", "timeout_seconds", "dry_run", "confirm_private_container", "expected_event_store_id"], context: "request")
    try requirePrivateWriteConfirmation(input)
    let entity = try Entity.parse(try requiredString(input, "entity"))
    let expectedFingerprint = try requiredString(input, "expected_fingerprint")
    let timeoutSeconds = try optionalInt(input, "timeout_seconds", default: 20, range: 5...60)
    let searchWindow = try optionalObject(input, "search_window").map { try DateWindow(input: $0, context: "search_window", maximumDays: 366) }
    if entity == .event, searchWindow == nil {
        throw BridgeFailure("validation_error", "search_window is required for Calendar event delete.")
    }
    let dryRun = try requiredBool(input, "dry_run")
    let expectedStoreID = try mutationExpectedStoreID(input, dryRun: dryRun)
    let (context, sourceID, containerID, itemID, scheduleID, locatorItem) = try parseItemLocator(input, entity: entity, writable: true)
    try requireExpectedStoreID(expectedStoreID, context: context)
    let calendar = try context.container(identifier: containerID, entity: entity, sourceID: sourceID, writable: true)
    let item = try requireUniqueCurrentSchedule(
        context: context,
        entity: entity,
        calendar: calendar,
        currentItem: locatorItem,
        scheduleID: scheduleID,
        searchWindow: searchWindow,
        timeoutSeconds: timeoutSeconds
    )
    let current = try requireExpectedFingerprint(item, entity: entity, expected: expectedFingerprint)
    if let reminder = item as? EKReminder, reminder.hasAlarms || !(reminder.alarms?.isEmpty ?? true) {
        throw BridgeFailure("unsupported_reminder_alarm", "Delete was refused because the Reminder contains alarms outside the managed contract.", details: ["current": current])
    }
    if let event = item as? EKEvent {
        if eventHasInvitation(event) {
            throw BridgeFailure("unsupported_invitation", "Calendar events with attendees or an organizer are not managed; delete was refused to avoid cancellation or invitation updates.", details: ["current": current])
        }
        if relativeAlarmMinutes(event) == nil {
            throw BridgeFailure("unsupported_event_alarm", "Delete was refused because the event contains an unsupported alarm.", details: ["current": current])
        }
        if event.structuredLocation != nil {
            throw BridgeFailure("unsupported_structured_location", "Delete was refused because the event contains a structured location.", details: ["current": current])
        }
    }
    if dryRun {
        emitSuccess([
            "command": "items delete",
            "dry_run": true,
            "event_store_id": context.store.eventStoreIdentifier,
            "current": current,
            "would_delete": ["entity": entity.rawValue, "item_id": itemID, "schedule_id": scheduleID],
            "mutated": false
        ])
        return
    }
    try requireExpectedStoreID(expectedStoreID, context: context)
    do {
        switch entity {
        case .reminder:
            try context.store.remove(item as! EKReminder, commit: true)
        case .event:
            try context.store.remove(item as! EKEvent, span: .thisEvent, commit: true)
        }
    } catch {
        throw eventKitFailure("eventkit_write_failed", "item delete", error)
    }
    context.store.reset()
    if fetchItem(store: context.store, entity: entity, itemID: itemID) != nil {
        throw BridgeFailure("write_unverified", "EventKit accepted delete, but the item still resolves during readback.", details: ["item_id": itemID, "schedule_id": scheduleID])
    }
    let readbackCalendar = try context.container(identifier: containerID, entity: entity, sourceID: sourceID, writable: true)
    if let remaining = try uniqueManagedMatch(
        context: context,
        entity: entity,
        calendar: readbackCalendar,
        scheduleID: scheduleID,
        searchWindow: searchWindow,
        timeoutSeconds: timeoutSeconds
    ) {
        throw BridgeFailure("write_unverified", "The requested item was deleted, but another item with the schedule_id remains; reconciliation is required.", details: ["remaining": try safeSnapshot(remaining, entity: entity)])
    }
    emitSuccess([
        "command": "items delete",
        "entity": entity.rawValue,
        "event_store_id": context.store.eventStoreIdentifier,
        "deleted": ["item_id": itemID, "schedule_id": scheduleID, "prior_fingerprint": expectedFingerprint],
        "verification": "local_eventkit_readback",
        "icloud_delivery_verified": false,
        "mutated": true
    ])
}

private func readbackExactItem(
    context: StoreContext,
    entity: Entity,
    sourceID: String,
    containerID: String,
    itemID: String
) throws -> EKCalendarItem {
    context.store.reset()
    guard let item = fetchItem(store: context.store, entity: entity, itemID: itemID) else {
        throw BridgeFailure("write_unverified", "EventKit accepted the write, but the item did not resolve during readback.", details: ["item_id": itemID])
    }
    guard item.calendar.calendarIdentifier == containerID, item.calendar.source.sourceIdentifier == sourceID else {
        throw BridgeFailure("item_moved", "The item moved during readback; reconciliation is required.")
    }
    return item
}

private func commandItemsClaim(_ input: [String: Any]) throws {
    try ensureAllowedKeys(input, ["operation_id", "entity", "source_id", "container_id", "item_id", "expected_fingerprint", "managed", "search_window", "timeout_seconds", "dry_run", "confirm_private_container", "expected_event_store_id"], context: "request")
    try requirePrivateWriteConfirmation(input)
    let operationID = try requiredString(input, "operation_id")
    try validateOperationIDShape(operationID)
    let entity = try Entity.parse(try requiredString(input, "entity"))
    let expectedFingerprint = try requiredString(input, "expected_fingerprint")
    let metadata = try ManagedMetadata(input: try requiredObject(input, "managed"), entity: entity)
    let timeoutSeconds = try optionalInt(input, "timeout_seconds", default: 20, range: 5...60)
    let searchWindow = try optionalObject(input, "search_window").map { try DateWindow(input: $0, context: "search_window", maximumDays: 366) }
    if entity == .event, searchWindow == nil {
        throw BridgeFailure("validation_error", "search_window is required when claiming a Calendar event.")
    }
    let dryRun = try requiredBool(input, "dry_run")
    let expectedStoreID = try mutationExpectedStoreID(input, dryRun: dryRun)
    let (context, sourceID, containerID, itemID, item) = try loadExactItem(input, entity: entity, writable: true)
    try requireExpectedStoreID(expectedStoreID, context: context)
    try requireUnmanagedMutationEligibility(item, entity: entity)
    if let event = item as? EKEvent, let searchWindow {
        guard searchWindow.start <= event.startDate, searchWindow.end >= event.endDate else {
            throw BridgeFailure("validation_error", "search_window must contain the Calendar event being claimed.")
        }
    }
    let calendar = try context.container(identifier: containerID, entity: entity, sourceID: sourceID, writable: true)
    if let existing = try uniqueManagedMatch(
        context: context,
        entity: entity,
        calendar: calendar,
        scheduleID: metadata.scheduleID,
        searchWindow: searchWindow,
        timeoutSeconds: timeoutSeconds
    ) {
        throw BridgeFailure("schedule_exists", "Another managed item already uses this schedule_id; claim was refused.", details: ["item": try safeSnapshot(existing, entity: entity)])
    }
    let current = try requireExpectedFingerprint(item, entity: entity, expected: expectedFingerprint)
    if dryRun {
        emitSuccess([
            "command": "items claim",
            "operation_id": operationID,
            "dry_run": true,
            "event_store_id": context.store.eventStoreIdentifier,
            "current": current,
            "would_add_managed": metadata.jsonObject,
            "mutated": false
        ])
        return
    }
    let expectedNotes = try mergeManagedBlock(existingNotes: item.notes, metadata: metadata)
    try requireExpectedStoreID(expectedStoreID, context: context)
    item.notes = expectedNotes
    switch entity {
    case .reminder: try saveReminder(item as! EKReminder, store: context.store, operation: "reminder claim")
    case .event: try saveEvent(item as! EKEvent, store: context.store, operation: "event claim")
    }
    let readback = try readbackExactItem(context: context, entity: entity, sourceID: sourceID, containerID: containerID, itemID: itemID)
    _ = try validateItemIdentity(readback, entity: entity, sourceID: sourceID, containerID: containerID, scheduleID: metadata.scheduleID)
    guard readback.notes == expectedNotes else {
        throw BridgeFailure("readback_mismatch", "Claim readback did not preserve marker-external notes.", details: ["fields": ["notes"]])
    }
    let readbackCalendar = try context.container(identifier: containerID, entity: entity, sourceID: sourceID, writable: true)
    let verified = try requireUniqueCurrentSchedule(
        context: context,
        entity: entity,
        calendar: readbackCalendar,
        currentItem: readback,
        scheduleID: metadata.scheduleID,
        searchWindow: searchWindow,
        timeoutSeconds: timeoutSeconds
    )
    guard verified.notes == expectedNotes else {
        throw BridgeFailure("readback_mismatch", "The final claim uniqueness readback did not preserve marker-external notes.", details: ["fields": ["notes"]])
    }
    emitSuccess([
        "command": "items claim",
        "operation_id": operationID,
        "event_store_id": context.store.eventStoreIdentifier,
        "item": try safeSnapshot(verified, entity: entity),
        "verification": "local_eventkit_readback",
        "icloud_delivery_verified": false,
        "mutated": true
    ])
}

private func commandUnmanagedItemsPatch(_ input: [String: Any]) throws {
    try ensureAllowedKeys(input, ["operation_id", "entity", "source_id", "container_id", "item_id", "expected_fingerprint", "payload", "search_window", "dry_run", "confirm_private_container", "expected_event_store_id"], context: "request")
    try requirePrivateWriteConfirmation(input)
    let operationID = try requiredString(input, "operation_id")
    try validateOperationIDShape(operationID)
    let entity = try Entity.parse(try requiredString(input, "entity"))
    let expectedFingerprint = try requiredString(input, "expected_fingerprint")
    let dryRun = try requiredBool(input, "dry_run")
    let expectedStoreID = try mutationExpectedStoreID(input, dryRun: dryRun)
    let searchWindow = try optionalObject(input, "search_window").map { try DateWindow(input: $0, context: "search_window", maximumDays: 366) }
    let reminderPayload: ReminderPayload?
    let eventPayload: EventPayload?
    switch entity {
    case .reminder:
        reminderPayload = try ReminderPayload(input: try requiredObject(input, "payload"), allowUserNotes: false)
        eventPayload = nil
    case .event:
        reminderPayload = nil
        eventPayload = try EventPayload(input: try requiredObject(input, "payload"), allowUserNotes: false)
        guard let searchWindow else {
            throw BridgeFailure("validation_error", "search_window is required for Calendar event patch.")
        }
        guard searchWindow.start <= eventPayload!.timing.start, searchWindow.end >= eventPayload!.timing.end else {
            throw BridgeFailure("validation_error", "search_window must contain the desired Calendar event time.")
        }
    }
    let (context, sourceID, containerID, itemID, item) = try loadExactItem(input, entity: entity, writable: true)
    try requireExpectedStoreID(expectedStoreID, context: context)
    try requireUnmanagedMutationEligibility(item, entity: entity)
    let current = try requireExpectedFingerprint(item, entity: entity, expected: expectedFingerprint)
    if dryRun {
        emitSuccess([
            "command": "unmanaged items patch",
            "operation_id": operationID,
            "dry_run": true,
            "event_store_id": context.store.eventStoreIdentifier,
            "current": current,
            "desired": reminderPayload?.safeJSON ?? eventPayload!.safeJSON,
            "mutated": false
        ])
        return
    }
    let expectedNotes = item.notes
    try requireExpectedStoreID(expectedStoreID, context: context)
    switch entity {
    case .reminder:
        let reminder = item as! EKReminder
        reminder.title = reminderPayload!.title
        reminder.dueDateComponents = reminderPayload!.due.dateComponents
        reminder.priority = reminderPayload!.priority
        try saveReminder(reminder, store: context.store, operation: "unmanaged reminder patch")
    case .event:
        let event = item as! EKEvent
        let payload = eventPayload!
        event.title = payload.title
        if event.location != payload.location { event.location = payload.location }
        event.startDate = payload.timing.start
        event.endDate = payload.timing.end
        event.isAllDay = payload.timing.allDay
        event.timeZone = payload.timing.timeZone
        event.alarms = payload.alarmMinutesBefore.map { EKAlarm(relativeOffset: -Double($0 * 60)) }
        try saveEvent(event, store: context.store, operation: "unmanaged event patch")
    }
    let readback = try readbackExactItem(context: context, entity: entity, sourceID: sourceID, containerID: containerID, itemID: itemID)
    guard ownershipInfo(readback.notes).classification == "unmanaged" else {
        throw BridgeFailure("readback_mismatch", "The one-shot patch unexpectedly changed item ownership.")
    }
    var mismatches: [String] = []
    if readback.notes != expectedNotes { mismatches.append("notes") }
    switch entity {
    case .reminder:
        let reminder = readback as! EKReminder
        if reminder.title != reminderPayload!.title { mismatches.append("title") }
        if !reminderDueMatches(reminderPayload!.due, reminder.dueDateComponents) { mismatches.append("due") }
        if reminder.priority != reminderPayload!.priority { mismatches.append("priority") }
    case .event:
        let event = readback as! EKEvent
        if event.title != eventPayload!.title { mismatches.append("title") }
        if event.location != eventPayload!.location { mismatches.append("location") }
        if event.isAllDay != eventPayload!.timing.allDay { mismatches.append("all_day") }
        if abs(event.startDate.timeIntervalSince(eventPayload!.timing.start)) >= 1.0 { mismatches.append("start") }
        if abs(event.endDate.timeIntervalSince(eventPayload!.timing.end)) >= 1.0 { mismatches.append("end") }
        if relativeAlarmMinutes(event) != eventPayload!.alarmMinutesBefore.sorted() { mismatches.append("alarms") }
    }
    if !mismatches.isEmpty {
        throw BridgeFailure("readback_mismatch", "The one-shot patch readback differs from the request.", details: ["fields": mismatches])
    }
    emitSuccess([
        "command": "unmanaged items patch",
        "operation_id": operationID,
        "event_store_id": context.store.eventStoreIdentifier,
        "item": try safeSnapshot(readback, entity: entity),
        "verification": "local_eventkit_readback",
        "icloud_delivery_verified": false,
        "mutated": true
    ])
}

private func commandUnmanagedRemindersComplete(_ input: [String: Any]) throws {
    try ensureAllowedKeys(input, ["operation_id", "source_id", "container_id", "item_id", "expected_fingerprint", "dry_run", "confirm_private_container", "expected_event_store_id"], context: "request")
    try requirePrivateWriteConfirmation(input)
    let operationID = try requiredString(input, "operation_id")
    try validateOperationIDShape(operationID)
    let expectedFingerprint = try requiredString(input, "expected_fingerprint")
    let dryRun = try requiredBool(input, "dry_run")
    let expectedStoreID = try mutationExpectedStoreID(input, dryRun: dryRun)
    let (context, sourceID, containerID, itemID, item) = try loadExactItem(input, entity: .reminder, writable: true)
    try requireExpectedStoreID(expectedStoreID, context: context)
    try requireUnmanagedMutationEligibility(item, entity: .reminder)
    let current = try requireExpectedFingerprint(item, entity: .reminder, expected: expectedFingerprint)
    let reminder = item as! EKReminder
    if reminder.isCompleted {
        emitSuccess(["command": "unmanaged reminders complete", "operation_id": operationID, "event_store_id": context.store.eventStoreIdentifier, "item": current, "already_completed": true, "mutated": false])
        return
    }
    if dryRun {
        emitSuccess(["command": "unmanaged reminders complete", "operation_id": operationID, "dry_run": true, "event_store_id": context.store.eventStoreIdentifier, "current": current, "would_set_completed": true, "mutated": false])
        return
    }
    let expectedNotes = reminder.notes
    try requireExpectedStoreID(expectedStoreID, context: context)
    reminder.isCompleted = true
    try saveReminder(reminder, store: context.store, operation: "unmanaged reminder complete")
    let readback = try readbackExactItem(context: context, entity: .reminder, sourceID: sourceID, containerID: containerID, itemID: itemID)
    guard let verified = readback as? EKReminder, verified.isCompleted, ownershipInfo(verified.notes).classification == "unmanaged", verified.notes == expectedNotes else {
        throw BridgeFailure("readback_mismatch", "The Reminder completion was not confirmed without changing ownership.")
    }
    emitSuccess([
        "command": "unmanaged reminders complete",
        "operation_id": operationID,
        "event_store_id": context.store.eventStoreIdentifier,
        "item": try safeSnapshot(verified, entity: .reminder),
        "verification": "local_eventkit_readback",
        "icloud_delivery_verified": false,
        "mutated": true
    ])
}

private func commandUnmanagedItemsDelete(_ input: [String: Any]) throws {
    try ensureAllowedKeys(input, ["operation_id", "entity", "source_id", "container_id", "item_id", "expected_fingerprint", "dry_run", "confirm_private_container", "expected_event_store_id"], context: "request")
    try requirePrivateWriteConfirmation(input)
    let operationID = try requiredString(input, "operation_id")
    try validateOperationIDShape(operationID)
    let entity = try Entity.parse(try requiredString(input, "entity"))
    let expectedFingerprint = try requiredString(input, "expected_fingerprint")
    let dryRun = try requiredBool(input, "dry_run")
    let expectedStoreID = try mutationExpectedStoreID(input, dryRun: dryRun)
    let (context, _, _, itemID, item) = try loadExactItem(input, entity: entity, writable: true)
    try requireExpectedStoreID(expectedStoreID, context: context)
    try requireUnmanagedMutationEligibility(item, entity: entity)
    let current = try requireExpectedFingerprint(item, entity: entity, expected: expectedFingerprint)
    if dryRun {
        emitSuccess(["command": "unmanaged items delete", "operation_id": operationID, "dry_run": true, "event_store_id": context.store.eventStoreIdentifier, "current": current, "mutated": false])
        return
    }
    try requireExpectedStoreID(expectedStoreID, context: context)
    do {
        switch entity {
        case .reminder: try context.store.remove(item as! EKReminder, commit: true)
        case .event: try context.store.remove(item as! EKEvent, span: .thisEvent, commit: true)
        }
    } catch {
        throw eventKitFailure("eventkit_write_failed", "unmanaged item delete", error)
    }
    context.store.reset()
    guard fetchItem(store: context.store, entity: entity, itemID: itemID) == nil else {
        throw BridgeFailure("write_unverified", "EventKit accepted delete, but the item still resolves.", details: ["item_id": itemID])
    }
    emitSuccess([
        "command": "unmanaged items delete",
        "operation_id": operationID,
        "event_store_id": context.store.eventStoreIdentifier,
        "deleted": ["item_id": itemID, "prior_fingerprint": expectedFingerprint],
        "verification": "local_eventkit_readback",
        "icloud_delivery_verified": false,
        "mutated": true
    ])
}

private func requireStableBundleIdentity() throws {
    guard Bundle.main.bundleIdentifier == expectedBundleIdentifier else {
        throw BridgeFailure(
            "bundle_identity_mismatch",
            "Run this binary through the public personal-scheduler executor so macOS can associate permissions with the stable app identity.",
            details: ["expected_bundle_id": expectedBundleIdentifier, "actual_bundle_id": Bundle.main.bundleIdentifier ?? NSNull()]
        )
    }
}

private func requireInternalInvocation() throws {
    guard ProcessInfo.processInfo.environment["PERSONAL_SCHEDULER_SWIFT_TOKEN"] == internalInvocationToken else {
        throw BridgeFailure("internal_entrypoint_required", "The EventKit app is internal; supported calls go through scripts/personal-scheduler.sh so they receive scope, preview, journal, and recovery checks.")
    }
}

@main
private struct PersonalSchedulerEventKitBridge {
    static func main() {
        do {
            try requireInternalInvocation()
            let arguments = Array(CommandLine.arguments.dropFirst())
            let command = arguments.joined(separator: " ")
            switch command {
            case "doctor", "status":
                try commandDoctor()
            case "self-test":
                try commandSelfTest()
            case "authorize":
                try requireStableBundleIdentity()
                let input = try readInput()
                try withProcessLock { try commandAuthorize(input) }
            case "sources list":
                try requireStableBundleIdentity()
                try commandSourcesList(try readInput())
            case "containers list":
                try requireStableBundleIdentity()
                try commandContainersList(try readInput())
            case "containers create":
                try requireStableBundleIdentity()
                let input = try readInput()
                try withProcessLock { try commandContainersCreate(input) }
            case "availability":
                try requireStableBundleIdentity()
                try commandAvailability(try readInput())
            case "events list":
                try requireStableBundleIdentity()
                try commandEventsList(try readInput())
            case "reminders list":
                try requireStableBundleIdentity()
                try commandRemindersList(try readInput())
            case "items find":
                try requireStableBundleIdentity()
                try commandItemsFind(try readInput())
            case "items get":
                try requireStableBundleIdentity()
                try commandItemsGet(try readInput())
            case "items create":
                try requireStableBundleIdentity()
                let input = try readInput()
                try withProcessLock { try commandItemsCreate(input) }
            case "items patch":
                try requireStableBundleIdentity()
                let input = try readInput()
                try withProcessLock { try commandItemsPatch(input) }
            case "reminders complete":
                try requireStableBundleIdentity()
                let input = try readInput()
                try withProcessLock { try commandRemindersComplete(input) }
            case "items delete":
                try requireStableBundleIdentity()
                let input = try readInput()
                try withProcessLock { try commandItemsDelete(input) }
            case "items claim":
                try requireStableBundleIdentity()
                let input = try readInput()
                try withProcessLock { try commandItemsClaim(input) }
            case "unmanaged items patch":
                try requireStableBundleIdentity()
                let input = try readInput()
                try withProcessLock { try commandUnmanagedItemsPatch(input) }
            case "unmanaged reminders complete":
                try requireStableBundleIdentity()
                let input = try readInput()
                try withProcessLock { try commandUnmanagedRemindersComplete(input) }
            case "unmanaged items delete":
                try requireStableBundleIdentity()
                let input = try readInput()
                try withProcessLock { try commandUnmanagedItemsDelete(input) }
            default:
                throw BridgeFailure(
                    "unknown_command",
                    "Unknown command.",
                    details: [
                        "received": command,
                        "supported": [
                            "doctor", "status", "self-test", "authorize", "sources list", "containers list", "containers create",
                            "availability", "events list", "reminders list", "items find", "items get", "items create", "items patch",
                            "reminders complete", "items delete", "items claim", "unmanaged items patch",
                            "unmanaged reminders complete", "unmanaged items delete"
                        ]
                    ]
                )
            }
        } catch let failure as BridgeFailure {
            emitFailure(failure)
        } catch {
            emitFailure(BridgeFailure("internal_error", "The bridge encountered an unexpected internal error.", details: ["type": String(describing: type(of: error))]))
        }
    }
}
