"""
Prescription scanner handler — EdgeOne Makers.
Route: POST /scanner
Accepts: { "image": "<base64>", "mimeType": "image/jpeg" }
Returns: JSON with extracted prescription fields
"""

import json
import anthropic
from .._model import resolve_model_name

client = anthropic.Anthropic()

EXTRACTION_PROMPT = """Extract the following from this prescription image and return ONLY valid JSON, no other text:
{
  "drug_name": null,
  "dosage": null,
  "frequency": null,
  "warnings": null,
  "prescribing_doctor": null
}
If a field is unreadable, keep it null."""

async def handler(ctx):
    body = ctx.request.body
    image_b64 = body.get("image", "")
    mime_type = body.get("mimeType", "image/jpeg")
    cid = ctx.conversation_id or ""

    if not image_b64:
        return {"error": "'image' is required"}

    message = client.messages.create(
        model=resolve_model_name(),
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": image_b64}},
                {"type": "text", "text": EXTRACTION_PROMPT}
            ]
        }]
    )

    raw_text = message.content[0].text
    try:
        extracted = json.loads(raw_text)
    except json.JSONDecodeError:
        return {"error": "failed to parse model output", "raw": raw_text}

    if cid:
        await ctx.store.append_message(conversation_id=cid, role="assistant", content=json.dumps(extracted))

    return extracted
