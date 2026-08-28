from __future__ import annotations

import json


CONTACT_FIELDS = {
    "first_name": "Legal first name",
    "last_name": "Legal last name",
    "email": "Email address",
    "phone": "Phone number",
    "address": "Street address",
    "city": "City",
    "province": "Province / state",
    "postal_code": "Postal / ZIP code",
    "linkedin_url": "LinkedIn URL",
    "portfolio_url": "Portfolio URL",
}


def form_fill_values(profile: dict) -> dict[str, str]:
    """Return only user-confirmed contact values suitable for text form fields."""
    return {key: str(profile.get(key, "")).strip() for key in CONTACT_FIELDS if str(profile.get(key, "")).strip()}


def build_form_fill_script(profile: dict) -> str:
    """Build a user-run browser-console script that fills text fields only.

    This deliberately contains no submit, click, checkbox, radio, file-upload, or
    select interaction. The user remains responsible for every review and submit.
    """
    values = json.dumps(form_fill_values(profile), ensure_ascii=False).replace("</", "<\\/")
    return f'''(() => {{
  "use strict";
  // CareerOS Form Fill Assistant. It fills text fields only and never submits.
  const values = {values};
  const aliases = {{
    first_name: ["first name", "given name", "firstname", "first_name", "legal first"],
    last_name: ["last name", "family name", "surname", "lastname", "last_name"],
    email: ["email", "e-mail"],
    phone: ["phone", "telephone", "mobile", "cell"],
    address: ["street address", "address line", "address"],
    city: ["city", "town"],
    province: ["province", "state", "region"],
    postal_code: ["postal", "zip", "postcode"],
    linkedin_url: ["linkedin", "linked in"],
    portfolio_url: ["portfolio", "website", "personal site"]
  }};
  const norm = (value) => String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  const fieldText = (field) => {{
    const id = field.id ? document.querySelector(`label[for="${{CSS.escape(field.id)}}"]`)?.innerText : "";
    return norm([field.name, field.id, field.autocomplete, field.placeholder, field.getAttribute("aria-label"), id].join(" "));
  }};
  const visible = (field) => !!(field.offsetWidth || field.offsetHeight || field.getClientRects().length);
  const editable = (field) => !field.disabled && !field.readOnly && visible(field) &&
    ((field.tagName === "TEXTAREA") || (field.tagName === "INPUT" && ["", "text", "email", "tel", "url", "search"].includes((field.type || "text").toLowerCase())));
  const fields = [...document.querySelectorAll("input, textarea")].filter(editable);
  const filled = [];
  for (const [key, value] of Object.entries(values)) {{
    const words = aliases[key];
    const target = fields.find((field) => !field.value && words.some((word) => fieldText(field).includes(norm(word))));
    if (!target) continue;
    const setter = Object.getOwnPropertyDescriptor(target.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype, "value")?.set;
    setter ? setter.call(target, value) : target.value = value;
    target.dispatchEvent(new Event("input", {{ bubbles: true }}));
    target.dispatchEvent(new Event("change", {{ bubbles: true }}));
    filled.push(key);
  }}
  alert(`CareerOS filled ${{filled.length}} text field(s): ${{filled.join(", ") || "none"}}. Nothing was submitted; review every field before submitting manually.`);
}})();'''
