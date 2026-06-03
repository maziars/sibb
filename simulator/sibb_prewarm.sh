#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
#  SIBB Simulator Pre-Warmer
#  Prepares a fresh iOS 26 simulator for benchmark episodes by:
#    1. Granting all privacy permissions to SIBB-11 apps (no permission dialogs)
#    2. Writing known first-launch suppression keys (no onboarding screens)
#    3. Launching each app briefly to trigger any remaining first-run setup
#    4. Watching for plist changes to discover unknown suppression keys
#
#  Run ONCE after creating a fresh simulator, before cloning the baseline.
#  Usage:
#    ./sibb_prewarm.sh <UDID>
#    ./sibb_prewarm.sh 19B95A95-614A-4ECA-B943-44FDADFD7A9F
# ═══════════════════════════════════════════════════════════════════════════

set -e
UDID="${1:-booted}"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

log()  { echo -e "${GREEN}[prewarm]${NC} $1"; }
warn() { echo -e "${YELLOW}[prewarm]${NC} $1"; }
err()  { echo -e "${RED}[prewarm]${NC} $1"; exit 1; }

# ── Verify simulator is booted ───────────────────────────────────────────────
STATUS=$(xcrun simctl list devices | grep "$UDID" | grep -o "Booted" || true)
if [ "$STATUS" != "Booted" ] && [ "$UDID" != "booted" ]; then
    err "Simulator $UDID is not booted. Run: xcrun simctl boot $UDID"
fi
log "Simulator: $UDID"

# ── Pin locale to en_US ──────────────────────────────────────────────────────
# Scaffold filters and several verifier strings (UIKit "Vertical scroll bar",
# Maps "Fastest" annotation, etc.) match English labels. If the sim defaults
# to a non-en locale, these silently 0%-match. The classifier regex in
# sibb_scaffold.py (_NOISE_OTHER_LABEL_RE) is en-US only by design — this
# pin ensures it stays applicable. See IOS_SIM_QUIRKS §20.
GLOBAL_PLIST="$HOME/Library/Developer/CoreSimulator/Devices/$UDID/data/Library/Preferences/.GlobalPreferences.plist"
if [ -f "$GLOBAL_PLIST" ]; then
    plutil -replace AppleLocale -string "en_US" "$GLOBAL_PLIST" 2>/dev/null || true
    plutil -replace AppleLanguages -json '["en-US"]' "$GLOBAL_PLIST" 2>/dev/null || true
    log "Locale pinned to en_US"
else
    warn "GlobalPreferences plist not found yet ($GLOBAL_PLIST); locale not pinned"
fi

# ── Disconnect Simulator.app hardware keyboard ──────────────────────────────
# Forces the on-screen iOS keyboard to appear on text-field focus, instead
# of routing host keystrokes through HID. Required for realistic typing.
# Simulator.app must be restarted for the change to take effect.
log "Disconnecting Simulator hardware keyboard (on-screen kb will show)..."
defaults write com.apple.iphonesimulator ConnectHardwareKeyboard -bool false

# ── SIBB-11 app bundle IDs ───────────────────────────────────────────────────
declare -a SIBB_APPS=(
    "com.apple.reminders"
    "com.apple.mobilecal"
    "com.apple.MobileAddressBook"
    "com.apple.Preferences"
    "com.apple.DocumentsApp"
    "com.apple.Health"
    "com.apple.Maps"
    "com.apple.mobileslideshow"
    "com.apple.shortcuts"
    "com.apple.mobilesafari"
    "com.apple.MobileSMS"
)

# ── Step 1: Grant all privacy permissions ────────────────────────────────────
log "Step 1: Granting privacy permissions to all SIBB apps..."

for bundle in "${SIBB_APPS[@]}"; do
    xcrun simctl privacy "$UDID" grant all "$bundle" 2>/dev/null && \
        log "  ✓ $bundle" || \
        warn "  ⚠ $bundle (may not support all permissions)"
done

# ── Step 2: Write first-launch suppression keys ──────────────────────────────
log "Step 2: Writing first-launch suppression keys..."

# Helper: write a defaults key inside the simulator
sim_defaults() {
    local domain="$1"
    local key="$2"
    local type="$3"
    local value="$4"
    xcrun simctl spawn "$UDID" defaults write "$domain" "$key" "$type" "$value" 2>/dev/null && \
        log "  ✓ $domain $key=$value" || \
        warn "  ⚠ Could not write $domain $key"
}

# Reminders
sim_defaults com.apple.reminders HaveShownWelcomeExperience    -bool YES
sim_defaults com.apple.reminders DidShowCloudKitMigrationDialog -bool YES
sim_defaults com.apple.reminders HasShownFirstLaunch            -bool YES

# Calendar
sim_defaults com.apple.mobilecal HaveShownWelcomeExperience    -bool YES
sim_defaults com.apple.mobilecal DidShowInitialRequest         -bool YES

# Health
sim_defaults com.apple.Health HaveShownWelcomeExperience       -bool YES
sim_defaults com.apple.Health onboardingComplete               -bool YES
sim_defaults com.apple.Health HSPAgreedToTermsOfService        -bool YES

# Maps
sim_defaults com.apple.Maps HaveShownWelcomeExperience         -bool YES
sim_defaults com.apple.Maps lastShownTrackingMode              -integer 0

# Photos
sim_defaults com.apple.mobileslideshow HaveAskedForPhotoPermissions -bool YES
sim_defaults com.apple.mobileslideshow HaveShownWelcomeExperience   -bool YES

# Files
sim_defaults com.apple.DocumentsApp HaveShownWelcomeExperience -bool YES

# Shortcuts
sim_defaults com.apple.shortcuts HaveShownWelcomeExperience    -bool YES
sim_defaults com.apple.shortcuts WelcomeSeen                   -bool YES

# Safari
sim_defaults com.apple.mobilesafari HaveShownWelcomeExperience -bool YES
sim_defaults com.apple.mobilesafari WarnAboutFraudulentWebsites -bool YES

# Messages (suppress iMessage setup)
sim_defaults com.apple.MobileSMS HaveShownWelcomeExperience    -bool YES

# Contacts
sim_defaults com.apple.MobileAddressBook HaveShownWelcomeExperience -bool YES

# Settings
sim_defaults com.apple.Preferences HaveShownWelcomeExperience  -bool YES

# Global: suppress iOS setup assistant and Siri
sim_defaults com.apple.SetupAssistant HaveShownWelcomeExperience -bool YES
sim_defaults com.apple.SetupAssistant LastInterruptionDate \
    -string "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ── Step 3: Launch each app briefly to flush any remaining first-launch logic ─
log "Step 3: Launching each app to trigger first-run (will auto-terminate)..."
warn "  This may show some dialogs — dismiss them manually if they appear."
warn "  After this step, save a clone as your baseline."

for bundle in "${SIBB_APPS[@]}"; do
    log "  Launching $bundle..."
    xcrun simctl launch "$UDID" "$bundle" 2>/dev/null || \
        warn "  ⚠ Could not launch $bundle"
    sleep 3   # give app time to complete first-launch logic
    xcrun simctl terminate "$UDID" "$bundle" 2>/dev/null || true
    sleep 1
done

log "Step 3 complete."

# ── Step 4: Discover unknown suppression keys (optional) ─────────────────────
log "Step 4: Discovering plist changes for iOS 26 key audit..."

SIM_PREFS="$HOME/Library/Developer/CoreSimulator/Devices/$UDID/data/Library/Preferences"
AUDIT_LOG="sibb_prewarm_audit_$(date +%Y%m%d_%H%M%S).txt"

if [ -d "$SIM_PREFS" ]; then
    echo "# SIBB Pre-warms Audit — $(date)" > "$AUDIT_LOG"
    echo "# These keys were written to simulator preferences during pre-warming" >> "$AUDIT_LOG"
    echo "" >> "$AUDIT_LOG"

    for plist in "$SIM_PREFS"/*.plist; do
        domain=$(basename "$plist" .plist)
        echo "## $domain" >> "$AUDIT_LOG"
        plutil -p "$plist" 2>/dev/null | grep -iE \
            "welcome|launch|onboard|shown|setup|first|intro|tutorial|icloud|sync" \
            >> "$AUDIT_LOG" 2>/dev/null || true
        echo "" >> "$AUDIT_LOG"
    done

    log "  Audit log written: $AUDIT_LOG"
    log "  Review this file to find iOS 26-specific suppression keys."
else
    warn "  Preferences directory not found — simulator may not be fully booted."
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
log "════════════════════════════════════════════════════════"
log "  Pre-warming complete."
log ""
log "  NEXT STEPS:"
log "  1. Manually open the Simulator app and check each of the 11 apps."
log "     Dismiss any remaining dialogs that appear."
log "  2. Review $AUDIT_LOG for undiscovered iOS 26 keys."
log "     Add any missing keys to this script under Step 2."
log "  3. Once all apps open cleanly, clone this simulator as your baseline:"
log "     xcrun simctl shutdown $UDID"
log "     xcrun simctl clone $UDID 'SIBB-Baseline'"
log "  4. Use SIBB-Baseline for all episode clones:"
log "     xcrun simctl clone SIBB-Baseline-UDID 'SIBB-Episode-001'"
log "════════════════════════════════════════════════════════"
