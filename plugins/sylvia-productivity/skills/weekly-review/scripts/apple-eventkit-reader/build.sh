#!/bin/zsh
set -euo pipefail
umask 077

script_dir=${0:A:h}
build_root="$script_dir/.build"
app_path="$build_root/WeeklyReviewEventKitReader.app"
contents_path="$app_path/Contents"
binary_path="$contents_path/MacOS/weekly-review-eventkit-reader"
source_path="$script_dir/Sources/main.swift"
plist_path="$script_dir/Info.plist"
bundle_id="io.github.sylviachenxy.sylvia-agent-skills.weekly-review-eventkit-reader"

machine_arch=$(/usr/bin/uname -m)
case "$machine_arch" in
  arm64|x86_64) ;;
  *)
    print -u2 "Unsupported build architecture: $machine_arch"
    exit 2
    ;;
esac

if [[ ! -f "$source_path" || ! -f "$plist_path" ]]; then
  print -u2 "Reader source or Info.plist is missing."
  exit 2
fi

/usr/bin/plutil -lint "$plist_path" >/dev/null
plist_bundle_id=$(/usr/bin/plutil -extract CFBundleIdentifier raw -o - "$plist_path")
if [[ "$plist_bundle_id" != "$bundle_id" ]]; then
  print -u2 "Unexpected bundle identifier in Info.plist."
  exit 2
fi
if [[ -L "$build_root" ]]; then
  print -u2 "The reader build root must not be a symbolic link."
  exit 2
fi
/bin/mkdir -p "$build_root"
/bin/chmod 700 "$build_root"
if [[ "$(/usr/bin/stat -f %u "$build_root")" != "$(/usr/bin/id -u)" || "$(/usr/bin/stat -f %Lp "$build_root")" != "700" ]]; then
  print -u2 "The reader build root failed owner or 0700 mode validation."
  exit 2
fi

is_fresh_build() {
  [[ -x "$binary_path" \
      && "$binary_path" -nt "$source_path" \
      && "$binary_path" -nt "$plist_path" \
      && "$binary_path" -nt "$script_dir/build.sh" ]] || return 1
  local minos signed_id
  minos=$(/usr/bin/otool -l "$binary_path" | /usr/bin/awk '/LC_BUILD_VERSION/{found=1} found && /minos/{print $2; exit}')
  signed_id=$(/usr/bin/codesign -dv --verbose=4 "$app_path" 2>&1 | /usr/bin/awk -F= '/^Identifier=/{print $2; exit}' || true)
  [[ "$minos" == "14.0" && "$signed_id" == "$bundle_id" ]] \
    && /usr/bin/codesign --verify --strict "$app_path" >/dev/null 2>&1 \
    && /usr/bin/lipo "$binary_path" -verify_arch "$machine_arch" >/dev/null 2>&1
}

if is_fresh_build; then
  print -r -- "$app_path"
  exit 0
fi

lock_dir="$build_root/build.lock"
deadline=$(( SECONDS + 30 ))
until /bin/mkdir "$lock_dir" 2>/dev/null; do
  if (( SECONDS >= deadline )); then
    print -u2 "Timed out waiting for the reader build lock."
    exit 4
  fi
  /bin/sleep 0.1
done

stage_root=""
cleanup() {
  if [[ -n "$stage_root" && -d "$stage_root" ]]; then
    /bin/rm -rf "$stage_root"
  fi
  /bin/rmdir "$lock_dir" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if is_fresh_build; then
  print -r -- "$app_path"
  exit 0
fi

stage_root=$(/usr/bin/mktemp -d "$build_root/stage.XXXXXX")
stage_app="$stage_root/WeeklyReviewEventKitReader.app"
stage_contents="$stage_app/Contents"
stage_binary="$stage_contents/MacOS/weekly-review-eventkit-reader"

/bin/mkdir -p "$stage_contents/MacOS" "$stage_contents/Resources"
/bin/cp "$source_path" "$stage_root/main.swift"
/bin/cp "$plist_path" "$stage_contents/Info.plist"

/usr/bin/xcrun swiftc \
  -target "$machine_arch-apple-macosx14.0" \
  -parse-as-library \
  -O \
  -warnings-as-errors \
  -framework EventKit \
  "$stage_root/main.swift" \
  -o "$stage_binary"

/usr/bin/codesign --force --sign - --identifier "$bundle_id" "$stage_app" >/dev/null

minos=$(/usr/bin/otool -l "$stage_binary" | /usr/bin/awk '/LC_BUILD_VERSION/{found=1} found && /minos/{print $2; exit}')
if [[ "$minos" != "14.0" ]]; then
  print -u2 "Unexpected deployment target: ${minos:-missing}; expected 14.0."
  exit 3
fi

if [[ -e "$app_path" ]]; then
  previous_app="$build_root/WeeklyReviewEventKitReader.previous.app"
  /bin/rm -rf "$previous_app"
  /bin/mv "$app_path" "$previous_app"
  /bin/mv "$stage_app" "$app_path"
  /bin/rm -rf "$previous_app"
else
  /bin/mv "$stage_app" "$app_path"
fi

print -r -- "$app_path"
