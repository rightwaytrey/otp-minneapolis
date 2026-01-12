# OTP Bicycle+Transit Fix - Documentation Index

This folder contains all documentation for the OpenTripPlanner bicycle+transit access fix.

## Quick Links

**Want to apply the fix?** → Start with `QUICK_START.md`

**Want to understand the problem?** → Read `README.md`

**Preparing a pull request?** → Use `PR_TEMPLATE.md`

**Curious about the investigation?** → Check `DEBUGGING_NOTES.md`

**Need the actual code changes?** → Apply `street-linker-bicycle-access.patch`

## File Descriptions

### 📋 README.md
**Comprehensive documentation of the fix**
- Problem statement and root cause analysis
- Complete code changes with before/after
- How the fix works (technical details)
- Testing procedures
- Impact analysis and considerations
- Future work suggestions

**Read this if:** You want complete technical documentation

### 🚀 QUICK_START.md
**Quick reference guide for applying and testing the fix**
- Two methods to apply the fix (git patch or manual)
- Build and test commands
- Verification steps

**Read this if:** You just want to get it working quickly

### 🐛 DEBUGGING_NOTES.md
**Investigation journey and lessons learned**
- All hypotheses tested (successful and failed)
- Key insights discovered
- Code deep dive
- Testing methodology
- Tools used

**Read this if:** You want to understand how we found the issue

### 📝 PR_TEMPLATE.md
**Template for submitting to OpenTripPlanner upstream**
- Problem description for external audience
- Solution explanation
- Impact analysis
- Discussion points for maintainers
- Backward compatibility notes
- Checklist for PR submission

**Read this if:** You're preparing to submit a pull request

### 🔧 street-linker-bicycle-access.patch
**Git patch file containing the code changes**
- Can be applied with `git apply`
- Shows exact diff of changes
- Generated from the working repository

**Use this if:** You want to apply the fix automatically

## Change Summary

### Code Changes
**One file modified:** `StreetLinkerModule.java`

**Three changes made:**
1. Added `WALK_AND_BICYCLE` constant (4 lines)
2. Changed stop linking from `WALK_ONLY` to `WALK_AND_BICYCLE` (1 line)
3. Updated JavaDoc comment (3 lines)

**Total diff:** +10 lines, -6 lines

### Configuration Changes
**One file required:** `build-config.json`

**Critical addition:**
```json
"transferRequests": [
  { "modes": "WALK" },
  { "modes": "BICYCLE" }
]
```

**Why:** Generates bicycle-mode transfers between stops during graph building

## Directory Structure

```
~/projects/opentripplanner/bicycle-transit-fix/
├── INDEX.md                              # This file
├── README.md                             # Complete technical documentation
├── QUICK_START.md                        # Quick reference guide
├── DEBUGGING_NOTES.md                    # Investigation process
├── PR_TEMPLATE.md                        # Pull request template
└── street-linker-bicycle-access.patch   # Git patch file
```

## Version History

| Date | Version | Changes |
|------|---------|---------|
| 2025-11-09 | 1.0 | Initial fix created and documented |

## Related Files (Not in This Folder)

**OTP Source Code:**
- `OpentripPlanner/application/src/main/java/org/opentripplanner/graph_builder/module/StreetLinkerModule.java` - **MODIFIED**

**Related OTP Files (Not Modified):**
- `application/src/main/java/org/opentripplanner/routing/linking/VertexLinker.java`
- `application/src/main/java/org/opentripplanner/street/model/edge/StreetTransitEntityLink.java`
- `application/src/main/java/org/opentripplanner/street/search/TraverseModeSet.java`

**OTP Build Output:**
- `OpentripPlanner/otp-shaded/target/otp-shaded-2.9.0-SNAPSHOT.jar` - Built JAR with fix

## Next Steps

1. **Apply code changes:** Modify `StreetLinkerModule.java` (see QUICK_START.md)
2. **Add build config:** Create `build-config.json` with bicycle transfers
3. **Rebuild graph:** Graph MUST be rebuilt with new config
4. **Test the fix:** Verify route 539 → 54 appears with bicycle mode
5. **Consider contributing:** Use `PR_TEMPLATE.md` to submit upstream

## Support & Questions

For questions about:
- **The fix itself:** See `README.md` technical details section
- **Applying the fix:** See `QUICK_START.md` troubleshooting
- **Contributing upstream:** See `PR_TEMPLATE.md` discussion points

## License

This fix is intended for contribution to OpenTripPlanner, which is licensed under LGPL v3.
