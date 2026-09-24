use framework "Foundation"
use scripting additions

-- Internal private JSON IPC for notes_publisher.py. No user text is interpolated
-- into source. Notes is contacted only after the complete authorization gate.
-- --self-test exercises only local values and never enters a Notes tell block.

property diagnosticsEnabled : false
property fixedStage : ""
property inputLimit : 3000000

on setStage(stageName)
    set fixedStage to stageName
    if diagnosticsEnabled then
        set messageText to current application's NSString's stringWithString:("MB_STAGE:" & stageName & linefeed)
        set messageData to messageText's dataUsingEncoding:(current application's NSUTF8StringEncoding)
        current application's NSFileHandle's fileHandleWithStandardError()'s writeData:messageData
    end if
end setStage

on failSafe(codeText)
    error codeText number 7001
end failSafe

on jsonText(objectValue)
    set jsonData to current application's NSJSONSerialization's dataWithJSONObject:objectValue options:0 |error|:(missing value)
    if jsonData is missing value then my failSafe("SERIALIZATION_ERROR")
    set resultText to current application's NSString's alloc()'s initWithData:jsonData encoding:(current application's NSUTF8StringEncoding)
    return resultText as text
end jsonText

on requireText(requestObject, keyName, maximumLength)
    set valueObject to requestObject's objectForKey:keyName
    if valueObject is missing value then my failSafe("INVALID_SCOPE")
    if not ((valueObject's isKindOfClass:(current application's NSString)) as boolean) then my failSafe("INVALID_SCOPE")
    set valueLength to valueObject's |length|() as integer
    if valueLength < 1 or valueLength > maximumLength then my failSafe("INVALID_SCOPE")
    set invalidRange to valueObject's rangeOfCharacterFromSet:(current application's NSCharacterSet's controlCharacterSet())
    if (|length| of invalidRange) > 0 then my failSafe("INVALID_SCOPE")
    return valueObject as text
end requireText

on validateRequest(requestObject)
    if requestObject is missing value then my failSafe("INVALID_REQUEST")
    if not ((requestObject's isKindOfClass:(current application's NSDictionary)) as boolean) then my failSafe("INVALID_REQUEST")
    set authorizationValue to requestObject's objectForKey:"authorized"
    if authorizationValue is missing value then my failSafe("AUTHORIZATION_REQUIRED")
    -- Canonical JSON distinguishes literal true from numeric 1 or a string.
    if my jsonText({authorizationValue}) is not "[true]" then my failSafe("AUTHORIZATION_REQUIRED")
    set actionName to my requireText(requestObject, "action", 16)
    if actionName is not "lookup" and actionName is not "create" then my failSafe("INVALID_ACTION")
    my requireText(requestObject, "account", 512)
    my requireText(requestObject, "folder", 512)
    my requireText(requestObject, "title", 300)
    if actionName is "create" then my requireText(requestObject, "body_html", inputLimit)
end validateRequest

on assertUniqueCount(matchCount, scopeKind)
    if matchCount = 0 then my failSafe(scopeKind & "_NOT_FOUND")
    if matchCount is not 1 then my failSafe(scopeKind & "_AMBIGUOUS")
end assertUniqueCount

on exactText(leftText, rightText)
    set leftValue to current application's NSString's stringWithString:leftText
    return ((leftValue's compare:rightText options:(current application's NSLiteralSearch)) as integer) = 0
end exactText

on exactNamedReferences(candidates, targetName)
    set exactMatches to {}
    repeat with candidateValue in candidates
        set selectedValue to contents of candidateValue
        tell application "Notes" to set candidateName to name of selectedValue
        -- AppleScript's default text comparison ignores case. Literal Foundation
        -- comparison ensures a broader native whose result never expands scope.
        if my exactText(candidateName, targetName) then set end of exactMatches to selectedValue
    end repeat
    return exactMatches
end exactNamedReferences

on readSelectedNote(selectedNote, targetTitle)
    my setStage("NOTE_SAFETY")
    tell application "Notes"
        if (shared of selectedNote) or (password protected of selectedNote) then my failSafe("UNSAFE_NOTE")
        my setStage("NOTE_TITLE_READ")
        if not my exactText(name of selectedNote, targetTitle) then my failSafe("TITLE_MISMATCH")
        my setStage("NOTE_BODY_READ")
        set htmlText to body of selectedNote
        my setStage("NOTE_PLAINTEXT_READ")
        set plainTextValue to plaintext of selectedNote
        my setStage("NOTE_ID_READ")
        set noteIdentifier to id of selectedNote
    end tell
    if (count characters of htmlText) > inputLimit or (count characters of plainTextValue) > 600000 then my failSafe("BODY_LIMIT_EXCEEDED")
    if (count characters of noteIdentifier) < 1 or (count characters of noteIdentifier) > 1024 then my failSafe("INVALID_NOTE_ID")
    return {note_id:noteIdentifier, title:targetTitle, body_html:htmlText, body_text:plainTextValue, shared:false, password_protected:false, truncated:false}
end readSelectedNote

on readMatchingNotes(selectedFolder, targetTitle)
    my setStage("NOTE_LOOKUP")
    tell application "Notes"
        set matchingNotes to every note of selectedFolder whose name is targetTitle
    end tell
    set matchingNotes to my exactNamedReferences(matchingNotes, targetTitle)
    set matchCount to count of matchingNotes
    if matchCount > 1 then return {{conflict:true}, {conflict:true}}
    if matchCount = 0 then return {}
    return {my readSelectedNote(item 1 of matchingNotes, targetTitle)}
end readMatchingNotes

on performRequest(requestObject)
    set targetAccount to my requireText(requestObject, "account", 512)
    set targetFolder to my requireText(requestObject, "folder", 512)
    set targetTitle to my requireText(requestObject, "title", 300)
    set actionName to my requireText(requestObject, "action", 16)
    my setStage("ACCOUNT_LOOKUP")
    tell application "Notes"
        set matchingAccounts to every account whose name is targetAccount
        set matchingAccounts to my exactNamedReferences(matchingAccounts, targetAccount)
        my assertUniqueCount(count of matchingAccounts, "ACCOUNT")
        set selectedAccount to item 1 of matchingAccounts
        if not my exactText(name of selectedAccount, targetAccount) then my failSafe("ACCOUNT_MISMATCH")
        my setStage("FOLDER_LOOKUP")
        set matchingFolders to every folder of selectedAccount whose name is targetFolder
        set matchingFolders to my exactNamedReferences(matchingFolders, targetFolder)
        my assertUniqueCount(count of matchingFolders, "FOLDER")
        set selectedFolder to item 1 of matchingFolders
        if not my exactText(name of selectedFolder, targetFolder) then my failSafe("FOLDER_MISMATCH")
        my setStage("FOLDER_SAFETY")
        if shared of selectedFolder then my failSafe("SHARED_FOLDER")
        if (id of container of selectedFolder) is not (id of selectedAccount) then my failSafe("FOLDER_NOT_DIRECT_CHILD")
    end tell
    set selectedNotes to my readMatchingNotes(selectedFolder, targetTitle)
    set createdFlag to false
    if actionName is "create" and (count of selectedNotes) = 0 then
        set htmlText to my requireText(requestObject, "body_html", inputLimit)
        my setStage("NOTE_CREATE")
        tell application "Notes"
            if shared of selectedFolder then my failSafe("SHARED_FOLDER")
            if not my exactText(name of selectedFolder, targetFolder) then my failSafe("FOLDER_MISMATCH")
            make new note at selectedFolder with properties {body:htmlText}
        end tell
        set createdFlag to true
        set selectedNotes to my readMatchingNotes(selectedFolder, targetTitle)
    end if
    my setStage("COMPLETE")
    return {ok:true, complete:true, truncated:false, notes:selectedNotes, created:createdFlag}
end performRequest

on safeError(errorText, errorNumber)
    if errorNumber = -1743 then return "AUTOMATION_NOT_AUTHORIZED"
    if errorNumber = -1712 then return "APPLE_EVENT_TIMEOUT"
    set safeCodes to {"INVALID_REQUEST", "INVALID_SCOPE", "INVALID_ACTION", "AUTHORIZATION_REQUIRED", "INPUT_LIMIT_EXCEEDED", "INVALID_JSON", "SERIALIZATION_ERROR", "UNEXPECTED_ARGUMENT", "ACCOUNT_NOT_FOUND", "ACCOUNT_AMBIGUOUS", "ACCOUNT_MISMATCH", "FOLDER_NOT_FOUND", "FOLDER_AMBIGUOUS", "FOLDER_MISMATCH", "SHARED_FOLDER", "FOLDER_NOT_DIRECT_CHILD", "UNSAFE_NOTE", "TITLE_MISMATCH", "BODY_LIMIT_EXCEEDED", "INVALID_NOTE_ID"}
    if errorNumber = 7001 and errorText is in safeCodes then return errorText
    return "NATIVE_ERROR"
end safeError

on selfTest()
    set checksPassed to true
    set testObject to current application's NSMutableDictionary's dictionary()
    testObject's setObject:true forKey:"authorized"
    testObject's setObject:"lookup" forKey:"action"
    testObject's setObject:"Fixture Account" forKey:"account"
    testObject's setObject:"Fixture Folder" forKey:"folder"
    testObject's setObject:"合成 <>& title" forKey:"title"
    my validateRequest(testObject)
    if my exactText("Fixture", "fixture") then set checksPassed to false
    if not my exactText("合成", "合成") then set checksPassed to false
    if my requireText(testObject, "title", 300) is not "合成 <>& title" then set checksPassed to false
    my assertUniqueCount(1, "ACCOUNT")
    try
        my assertUniqueCount(2, "ACCOUNT")
        set checksPassed to false
    on error errorText number errorNumber
        if my safeError(errorText, errorNumber) is not "ACCOUNT_AMBIGUOUS" then set checksPassed to false
    end try
    try
        my assertUniqueCount(0, "FOLDER")
        set checksPassed to false
    on error errorText number errorNumber
        if my safeError(errorText, errorNumber) is not "FOLDER_NOT_FOUND" then set checksPassed to false
    end try
    if my safeError("PRIVATE_EXCEPTION_TEXT", 12345) is not "NATIVE_ERROR" then set checksPassed to false
    set noteFixture to {note_id:"fixture-note", title:"Fixture", body_html:"<div>Fixture</div>", body_text:"Fixture", shared:false, password_protected:false, truncated:false}
    set fixtureJSON to current application's NSString's stringWithString:(my jsonText(noteFixture))
    set fixtureData to fixtureJSON's dataUsingEncoding:(current application's NSUTF8StringEncoding)
    set decodedFixture to current application's NSJSONSerialization's JSONObjectWithData:fixtureData options:0 |error|:(missing value)
    repeat with fixtureKey in {"note_id", "title", "body_html", "body_text", "shared", "password_protected", "truncated"}
        if (decodedFixture's objectForKey:(contents of fixtureKey)) is missing value then set checksPassed to false
    end repeat
    testObject's setObject:1 forKey:"authorized"
    try
        my validateRequest(testObject)
        set checksPassed to false
    on error errorText number errorNumber
        if my safeError(errorText, errorNumber) is not "AUTHORIZATION_REQUIRED" then set checksPassed to false
    end try
    return my jsonText({ok:checksPassed, fixture_only:true, native_app_contacted:false, checks:9})
end selfTest

on run argv
    if (count of argv) = 1 and item 1 of argv is "--self-test" then return my selfTest()
    try
        if (count of argv) is not 0 then my failSafe("UNEXPECTED_ARGUMENT")
        set diagnosticsEnabled to true
        my setStage("STDIN_READING")
        set inputData to current application's NSFileHandle's fileHandleWithStandardInput()'s readDataToEndOfFile()
        if (inputData's |length|() as integer) > inputLimit then my failSafe("INPUT_LIMIT_EXCEEDED")
        set requestObject to current application's NSJSONSerialization's JSONObjectWithData:inputData options:0 |error|:(missing value)
        if requestObject is missing value then my failSafe("INVALID_JSON")
        my setStage("INPUT_PARSED")
        my validateRequest(requestObject)
        my setStage("INPUT_VALIDATED")
        return my jsonText(my performRequest(requestObject))
    on error errorText number errorNumber
        -- Native exception text is never returned or logged.
        return my jsonText({ok:false, code:my safeError(errorText, errorNumber)})
    end try
end run
