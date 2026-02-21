# Force Full Physical Mouse/Keyboard Chain

This project now supports a strict execution profile that avoids DOM fallback unless you explicitly opt in.

## Goal

- Do not enable DOM execution by default.
- When force profile is enabled, run the operation chain with physical mouse/keyboard behavior only.

## UI Entry

In `dashboard.py`:

- Open `Unified Language and Reply Settings` -> `Human-like Execution Parameters`.
- Use `Full Physical Preset` or enable:
  - `Force Full Physical Mouse/Keyboard Execution`

When enabled, apply will force:

- `operation_execution_mode = ocr_vision`
- `web_info_source_mode = screen_ocr`
- physical click preference on
- keyboard-only message send path on
- DOM fallback off

## Environment Variables

Recommended defaults:

```env
OPERATION_EXECUTION_MODE=ocr_vision
WEB_INFO_SOURCE_MODE=ocr_only
OCR_VISION_ALLOW_DOM_FALLBACK=false
FORCE_FULL_PHYSICAL_MOUSE_KEYBOARD=false
```

Strict profile:

```env
FORCE_FULL_PHYSICAL_MOUSE_KEYBOARD=true
OPERATION_EXECUTION_MODE=ocr_vision
WEB_INFO_SOURCE_MODE=screen_ocr
OCR_VISION_ALLOW_DOM_FALLBACK=false
```

## Runtime Behavior

- DOM mode is treated as explicit opt-in.
- Legacy runtime state with `dom` but without explicit opt-in flags is auto-downgraded:
  - operation mode -> `ocr_vision`
  - web info mode -> `ocr_only`
- In force profile:
  - no JS click fallback for OCR click path
  - no JS form submit fallback in send-message chain
  - no automatic DOM action fallback when OCR anchor is not confirmed
