import anthropic
import base64
import json
import re
import os
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

EXTRACTION_PROMPT = """Extract all rows from this handwritten Arabic leads table into JSON.
Each row must have: phone, name, level, city, status.
Rules:
- Remove all dashes and spaces from phone numbers
- If status contains RV, RDV, R.D.V → set status to 'RDV'
- If status contains B.V, boite vocal → set status to 'B.V'
- If status contains N.R, NRP → set status to 'N.R'
- If status contains P.I, pas intéressé → set status to 'P.I'
- If status contains autre ville, autre city → set status to 'Autre ville'
- Phone numbers must be 10 digits starting with 06 or 07
- Flag any phone number that does not match this pattern with "flagged": true
- Return only valid JSON array, no extra text"""


def validate_phone(phone: str) -> bool:
    phone = re.sub(r'[\s\-]', '', phone)
    return bool(re.match(r'^0[67]\d{8}$', phone))


def clean_phone(phone: str) -> str:
    return re.sub(r'[\s\-]', '', phone)


async def extract_leads_from_image(image_bytes: bytes, media_type: str) -> list:
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": EXTRACTION_PROMPT
                    }
                ],
            }
        ],
    )

    raw = message.content[0].text.strip()

    # Extract JSON array from response
    match = re.search(r'\[.*\]', raw, re.DOTALL)
    if not match:
        raise ValueError("Claude did not return a valid JSON array")

    leads = json.loads(match.group())

    # Post-process each lead
    for lead in leads:
        phone = clean_phone(str(lead.get("phone", "")))
        lead["phone"] = phone
        lead["flagged"] = not validate_phone(phone)

    return leads
