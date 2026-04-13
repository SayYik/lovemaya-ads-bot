#!/usr/bin/env python3
BOT_VERSION = "v4.5"  # Change this to verify Railway deploys the latest file
"""
Lovemaya Meta Ads Bot
======================
Telegram bot that turns a short brief into a full Meta ad campaign.

Flow:
  You (Telegram) → Claude (generates campaign) → Meta API (creates campaign)
                                                → Manus (optional browser execution)

Usage:
  python telegram_bot.py

Environment variables (set in .env):
  TELEGRAM_BOT_TOKEN   - from @BotFather
  CLAUDE_API_KEY       - from console.anthropic.com
  META_ACCESS_TOKEN    - from Meta Business (see SETUP_GUIDE.md)
  META_AD_ACCOUNT_ID   - e.g. act_752480016788280
  META_PAGE_ID         - your Facebook Page ID
  MANUS_API_KEY        - (optional) from Manus AI
  ALLOWED_USER_IDS     - comma-separated Telegram user IDs that can use this bot

Requirements:
  pip install python-telegram-bot anthropic requests python-dotenv
"""

import os
import json
import logging
import html
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
import anthropic
import requests

# ─────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "")
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
META_PAGE_ID = os.getenv("META_PAGE_ID", "")
META_IG_ACTOR_ID = os.getenv("META_IG_ACTOR_ID", "")
MANUS_API_KEY = os.getenv("MANUS_API_KEY", "")
TOGETHER_API_KEY = os.getenv("TOGETHER_API_KEY", "")  # For AI image generation
ALLOWED_USER_IDS = [int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()]

# ─────────────────────────────────────────────
# AD ACCOUNTS — add/edit your accounts here
# ─────────────────────────────────────────────
AD_ACCOUNTS = {
    "cpas_sg": {
        "id": "act_2989596441217531",
        "name": "CPAS SG",
        "keywords": ["cpas sg", "singapore", "sg", "cpas singapore"],
        "country": "SG",
        "currency": "SGD",
    },
    "cpas_my": {
        "id": "act_649067021263977",
        "name": "CPAS MY",
        "keywords": ["cpas my", "cpas malaysia", "cpas"],
        "country": "MY",
        "currency": "MYR",
    },
    "shopify_my": {
        "id": "act_752480016788280",
        "name": "Shopify MY",
        "keywords": ["shopify", "shopify my", "shopify malaysia", "website", "web"],
        "country": "MY",
        "currency": "MYR",
    },
}
DEFAULT_AD_ACCOUNT = "shopify_my"  # Used when no account is mentioned


def detect_ad_account(text: str) -> dict:
    """Detect which ad account to use based on keywords in the brief."""
    text_lower = text.lower()
    for key, account in AD_ACCOUNTS.items():
        for keyword in sorted(account["keywords"], key=len, reverse=True):
            if keyword in text_lower:
                logger.info(f"Ad account detected: {account['name']} (matched '{keyword}')")
                return account
    default = AD_ACCOUNTS[DEFAULT_AD_ACCOUNT]
    logger.info(f"No ad account keyword found, using default: {default['name']}")
    return default


# ─────────────────────────────────────────────
# BUDGET TYPE — CBO vs ABO
# ─────────────────────────────────────────────

def detect_budget_type(text: str) -> str:
    """
    Detect budget type from the brief.
    CBO = Campaign Budget Optimization (budget set at campaign level, shared across ad sets)
    ABO = Ad Set Budget Optimization (budget set per ad set, default)
    """
    text_lower = text.lower()
    cbo_keywords = ["cbo", "campaign budget", "campaign level budget", "shared budget"]
    abo_keywords = ["abo", "adset budget", "ad set budget"]

    for kw in cbo_keywords:
        if kw in text_lower:
            logger.info(f"Budget type detected: CBO (matched '{kw}')")
            return "CBO"
    for kw in abo_keywords:
        if kw in text_lower:
            logger.info(f"Budget type detected: ABO (matched '{kw}')")
            return "ABO"

    logger.info("No budget type keyword found, using default: ABO")
    return "ABO"


# ─────────────────────────────────────────────
# AUDIENCE TYPE — Advantage+ vs Manual Targeting
# ─────────────────────────────────────────────

def detect_audience_type(text: str) -> str:
    """
    Detect audience type from the brief.
    ADV+  = Advantage+ Audience (Meta AI expands your targeting automatically)
    MANUAL = Manual Targeting (use exact targeting you specify, default)

    Keywords:
    - "adv+", "advantage+", "advantage audience", "broad targeting", "ai audience" → ADV+
    - "manual targeting", "exact targeting", "adv-", "no advantage" → MANUAL
    """
    text_lower = text.lower()
    adv_keywords = ["adv+", "advantage+", "advantage audience", "broad targeting", "ai audience", "advantage plus"]
    manual_keywords = ["manual targeting", "exact targeting", "adv-", "no advantage", "manual audience"]

    for kw in adv_keywords:
        if kw in text_lower:
            logger.info(f"Audience type detected: ADV+ (matched '{kw}')")
            return "ADV+"
    for kw in manual_keywords:
        if kw in text_lower:
            logger.info(f"Audience type detected: MANUAL (matched '{kw}')")
            return "MANUAL"

    logger.info("No audience type keyword found, using default: MANUAL")
    return "MANUAL"


# Store pending campaigns waiting for approval
pending_campaigns = {}

# ─────────────────────────────────────────────
# DTC PERSONAL CARE KNOWLEDGE BASE
# ─────────────────────────────────────────────

DTC_KNOWLEDGE = """
DTC PERSONAL CARE BRAND INTELLIGENCE (learned from top brands like Glossier, Dr. Squatch, Sol de Janeiro, CeraVe, Native, Lush, Drunk Elephant, Nécessaire, Billie, The Ordinary):

AD FORMAT BEST PRACTICES:
- Carousel Ads: 30-50% lower cost per conversion than single images. Best ROAS. Use for product collections, routine steps, before/after.
- Static Images: Best for prospecting efficiency (low CPM/CPC). Recommended mix: 60% static + 40% video.
- Video Ads: 6-15s for awareness, 15-30s for testimonials. Highest engagement but costlier.
- Produce 10-15 new creative variations monthly to combat ad fatigue.

PROVEN AD COPY ANGLES FOR BODY CARE:
1. BENEFIT-LED: Focus on ONE specific outcome (hydration, glow, softness, scent longevity). "Skin so soft, you can't stop touching it"
2. SENSORY/EMOTIONAL: Describe fragrance and texture to create anticipation. "Like a tropical vacation in a bottle"
3. INGREDIENT-FOCUSED: Highlight hero ingredients. "Powered by shea butter & vitamin E"
4. SOCIAL PROOF: Reviews, ratings, before/after. "Join 50,000+ women who switched"
5. ROUTINE-BASED: Show product as part of daily ritual. "Your new 2-minute glow routine"
6. URGENCY/SCARCITY: Limited editions, seasonal scents. "New scent — only 500 bottles"
7. VALUE/BUNDLE: Emphasize savings. "Get the full set — save 30%"
8. FOUNDER STORY: Personal authenticity. "I created this because I couldn't find..."
9. COMPARE & SWITCH: Position against alternatives. "Why 10,000 women switched from [generic]"
10. UGC/TESTIMONIAL: Real customer voice. "I've tried everything — this actually works"

FUNNEL STRUCTURE FOR DTC BODY CARE:
- AWARENESS (top): Educational content, brand story, ingredient spotlight, founder content
  → Objective: OUTCOME_AWARENESS, optimize: REACH
  → Budget: 30-40% of total
- CONSIDERATION (mid): Tutorials, comparisons, testimonials, routine videos
  → Objective: OUTCOME_TRAFFIC, optimize: LINK_CLICKS
  → Budget: 30-40% of total
- CONVERSION (bottom): Strong offers, dynamic product ads, urgency
  → Objective: OUTCOME_SALES, optimize: OFFSITE_CONVERSIONS
  → Budget: 20-30% of total

SEASONAL CAMPAIGN IDEAS:
- JAN-FEB: "New Year Glow Up" — renewal messaging, self-care resolutions
- MAR-APR: "Spring Fresh" — lightweight formulas, floral scents, renewal
- MAY-JUN: "Summer Ready" — body mist, SPF, lightweight hydration
- JUL-AUG: "Glow Season" — shimmer, tropical scents, beach-ready
- SEP-OCT: "Back to Routine" — skincare essentials, bundles, early holiday teasers
- NOV-DEC: "Holiday Gifting" — gift sets, limited editions, BOGO, 12 Days of Deals

TARGETING BEST PRACTICES (2025-2026):
- Meta now favors BROAD targeting over narrow segments (audiences 10M+ perform best)
- Advantage+ (AI audience) often outperforms manual targeting for prospecting
- Manual targeting works better for retargeting warm audiences
- Budget split: 70-80% prospecting (cold), 20-30% retargeting (warm)

WINNING STRATEGIES FROM TOP BRANDS:
- Sol de Janeiro: Bold colors, sensory marketing, strong scent storytelling
- CeraVe: Dermatologist partnerships, educational content, ingredient transparency
- Glossier: Minimalist aesthetic, community-driven UGC, aspirational simplicity
- Dr. Squatch: Aggressive video content, humor, masculine self-care positioning
- Lush: UGC-first (500K+ hashtag mentions/month), values-driven messaging
- Native: Visual-first content, 100+ active ads, always-on testing approach
"""

# ─────────────────────────────────────────────
# LEARNING MEMORY — Bot remembers your preferences
# ─────────────────────────────────────────────
MEMORY_FILE = os.path.join(os.path.dirname(__file__), "bot_memory.json")

def load_memory() -> list:
    """Load saved feedback/preferences."""
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return []

def save_memory(memories: list):
    """Save feedback/preferences to file."""
    try:
        with open(MEMORY_FILE, "w") as f:
            json.dump(memories, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save memory: {e}")

def add_memory(feedback: str):
    """Add a new feedback entry."""
    memories = load_memory()
    memories.append({
        "feedback": feedback,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    # Keep only last 50 memories
    if len(memories) > 50:
        memories = memories[-50:]
    save_memory(memories)

def get_memory_prompt() -> str:
    """Build a memory context string for Claude."""
    memories = load_memory()
    if not memories:
        return ""
    memory_lines = []
    for m in memories[-20:]:  # Use last 20 most recent
        memory_lines.append(f"- {m['feedback']} ({m['date']})")
    return "\n\nUSER PREFERENCES & PAST FEEDBACK (learn from these):\n" + "\n".join(memory_lines)

# ─────────────────────────────────────────────
# LOVEMAYA BRAND CONTEXT (sent to Claude)
# ─────────────────────────────────────────────

BRAND_SYSTEM_PROMPT = """You are the Meta Ads Engine for Lovemaya. When given a brief, generate a COMPLETE campaign in JSON format.

BRAND INFO:
- Business: Lovemaya
- Website: https://lovemaya.co
- Instagram: @lovemaya.my
- Industry: Beauty & personal care / body care
- Default currency: MYR (Malaysian Ringgit)
- Default country: Malaysia
- Tone: Elegant, fresh & natural, affordable luxury

AD ACCOUNTS (user picks one in their brief):
- "CPAS SG" (act_2989596441217531) — for Singapore CPAS campaigns, currency: SGD
- "CPAS MY" (act_649067021263977) — for Malaysia CPAS campaigns, currency: MYR
- "Shopify MY" (act_752480016788280) — for Shopify Malaysia website campaigns, currency: MYR
If the user mentions "cpas sg" or "singapore" → use CPAS SG account.
If the user mentions "cpas my" or "cpas" → use CPAS MY account.
If the user mentions "shopify" or "website" → use Shopify MY account.
If not mentioned, default to Shopify MY.
Include the ad_account field in your JSON response with the account key: "cpas_sg", "cpas_my", or "shopify_my".
- Ad Account: act_752480016788280

OBJECTIVE SELECTION GUIDE:
Choose the best objective based on the brief's goal. If the user mentions a goal, map it like this:
- "traffic" / "website visits" / "clicks" → OUTCOME_TRAFFIC (optimize: LINK_CLICKS)
- "sales" / "conversions" / "purchase" → OUTCOME_SALES (optimize: OFFSITE_CONVERSIONS)
- "awareness" / "reach" / "branding" → OUTCOME_AWARENESS (optimize: REACH)
- "leads" / "sign up" / "form" → OUTCOME_LEADS (optimize: LEAD_GENERATION)
- "engagement" / "likes" / "comments" → OUTCOME_ENGAGEMENT (optimize: POST_ENGAGEMENT)
If the user doesn't specify a goal, choose the best objective based on the product and context.

LANGUAGE STRATEGY — DYNAMIC, USER-CONTROLLED:
The user specifies which languages they want in their brief. Generate ONE ad variant per language requested.

How to detect languages from the brief:
- "english" / "EN" → English
- "malay" / "bahasa" / "BM" / "bahasa malaysia" → Bahasa Malaysia
- "chinese" / "CN" / "mandarin" / "中文" → Chinese (Simplified / 简体中文)
- "tamil" / "TM" → Tamil
- "arabic" / "AR" → Arabic
- "korean" / "KR" / "한국어" → Korean
- "japanese" / "JP" / "日本語" → Japanese
- "thai" / "TH" → Thai
- "indonesian" / "indo" / "BI" → Bahasa Indonesia
- "hindi" / "HI" → Hindi
- Any other language the user mentions → use that language

If the user does NOT mention any languages, default to 3 variants: English, Bahasa Malaysia, Chinese (Simplified).

TONE GUIDE per language:
- English → clean, modern, aspirational
- Bahasa Malaysia → warm, relatable, casual (not formal)
- Chinese (简体中文) → elegant, concise, beauty-focused
- Tamil → respectful, family-oriented, warm
- For other languages → match the Lovemaya brand tone (elegant, natural, affordable luxury)

Each variant should convey the SAME core message/offer but LOCALIZED naturally (not a direct translation — adapt the feel for that audience and culture).

IMPORTANT DATE: Today's date will be provided at the end of the brief. ALWAYS use that date for the campaign name MonthYear — NEVER use old dates like 2024.

INTEREST TARGETING — USER-CONTROLLED:
- If the user specifies interests in the brief (e.g. "interest: skincare, K-beauty, perfume") → use ONLY those exact interests
- If the user does NOT specify interests → choose 3-5 relevant interests for the product
- Always include the interests in the JSON so the user can review them before approving

RESPOND WITH VALID JSON ONLY (no markdown, no ```). Use this exact structure:

{
  "campaign_name": "Lovemaya_[Product]_[Objective]_[MonthYear e.g. Apr2026]",
  "ad_account": "shopify_my",
  "objective": "OUTCOME_TRAFFIC",
  "currency": "MYR",
  "website_url": "https://lovemaya.co",
  "adset": {
    "name": "[descriptive ad set name]",
    "daily_budget": 1000,
    "age_min": 18,
    "age_max": 45,
    "gender": "women",
    "optimization_goal": "LINK_CLICKS",
    "locations": ["Malaysia"],
    "interests": ["Beauty", "Fragrance"],
    "languages": ["en", "zh_CN"]
  },
  "ad_variants": [
    {
      "name": "Variant_A_[LANG_CODE]",
      "language": "[Language Name]",
      "primary_text": "under 125 chars — in that language",
      "headline": "under 40 chars — in that language",
      "description": "under 30 chars — in that language",
      "cta": "SHOP_NOW",
      "angle": "[angle]"
    }
  ],
  "image_prompts": [
    "Image option 1: [detailed prompt — product-focused, clean lifestyle shot, 1:1 ratio]",
    "Image option 2: [detailed prompt — sensory/emotional mood, warm colors, 1:1 ratio]",
    "Image option 3: [detailed prompt — bold/eye-catching, creative composition, 1:1 ratio]"
  ],
  "policy_check": "No policy issues found.",
  "manus_instructions": "Step-by-step instructions for Manus AI to create this in Meta Ads Manager...",
  "summary": "A short 2-3 line summary for the Telegram reply"
}

RULES:
- ONLY create ONE campaign, ONE ad set, and one ad per language. Never duplicate.
- Generate ONE variant per language the user requests (could be 1, 2, 3, 4, or more)
- If no languages specified, default to 3: English, Bahasa Malaysia, Chinese (Simplified)
- Each variant is LOCALIZED (not a direct translation) — adapt the feel for that audience
- Use different angles for each variant (benefit-led, emotional, urgency, social-proof, etc.)
- Primary text under 125 chars, headline under 40, description under 30
- CTA must be: SHOP_NOW, LEARN_MORE, SIGN_UP, BOOK_NOW, or GET_OFFER
- ALWAYS include the "language" field in each variant
- Include detailed Manus instructions with exact button clicks and field values
- Check against Meta Advertising Standards and flag any risks in policy_check
- Match the budget, targeting, and objective from the user's brief
- Choose the RIGHT objective based on the goal (see OBJECTIVE SELECTION GUIDE above)
- Budget is in MYR (Malaysian Ringgit). MYR 10/day = daily_budget: 1000 (Meta uses cents)
- Default targeting: Malaysia. Use specific states/cities if the user mentions them
- If the brief is missing info, use sensible Lovemaya defaults for Malaysian market
- BUDGET TYPE: If user says "CBO" or "campaign budget" → Campaign Budget Optimization. If "ABO" or "adset budget" → Ad Set Budget. Default is ABO.
- AUDIENCE TYPE: If user says "adv+" or "advantage+" → Advantage+ Audience (Meta AI expands targeting). If "manual targeting" or "adv-" → Manual Targeting (exact targeting). Default is Manual.
- INTERESTS: If user specifies interests (e.g. "interest: skincare, perfume") → use ONLY those. If not specified → pick 3-5 relevant ones for the product.
- LANGUAGES field in adset: Include Meta locale codes for the languages in your ad variants. Map: English→"en", Malay→"ms", Chinese→"zh_CN", Tamil→"ta", Arabic→"ar", Korean→"ko", Japanese→"ja", Thai→"th", Indonesian→"id", Hindi→"hi"
"""


# ─────────────────────────────────────────────
# ACCESS CONTROL
# ─────────────────────────────────────────────

def is_authorized(user_id: int) -> bool:
    """Check if user is allowed to use this bot."""
    if not ALLOWED_USER_IDS:
        return True  # No restrictions if not configured
    return user_id in ALLOWED_USER_IDS


# ─────────────────────────────────────────────
# CLAUDE AI — CAMPAIGN GENERATOR
# ─────────────────────────────────────────────

def generate_campaign_with_claude(brief_text: str) -> dict:
    """Send brief to Claude API and get structured campaign JSON."""
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    # Add DTC knowledge + learned preferences to the system prompt
    full_system = BRAND_SYSTEM_PROMPT + "\n" + DTC_KNOWLEDGE + get_memory_prompt()

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        system=full_system,
        messages=[
            {"role": "user", "content": f"Create a Meta ad campaign for this brief:\n\n{brief_text}\n\n[Today's date: {datetime.now().strftime('%B %Y')}]"}
        ]
    )

    response_text = message.content[0].text.strip()

    # Parse JSON from response
    # Handle cases where Claude might wrap in ```json
    if response_text.startswith("```"):
        response_text = response_text.split("\n", 1)[1]
        response_text = response_text.rsplit("```", 1)[0]

    campaign = json.loads(response_text)
    return campaign


# ─────────────────────────────────────────────
# META ADS API — CAMPAIGN CREATOR
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# AI IMAGE & VIDEO GENERATION (Higgsfield AI + Together AI fallback)
# ─────────────────────────────────────────────

HIGGSFIELD_API_KEY = os.getenv("HIGGSFIELD_API_KEY", "")
LOGO_PATH = os.path.join(os.path.dirname(__file__), "logo.png")  # Put your logo.png here


def add_logo_to_image(image_path: str) -> str:
    """Overlay the Lovemaya logo on the bottom-right of the image."""
    if not os.path.exists(LOGO_PATH):
        logger.info("No logo.png found — skipping logo overlay")
        return image_path

    try:
        from PIL import Image
        img = Image.open(image_path).convert("RGBA")
        logo = Image.open(LOGO_PATH).convert("RGBA")

        # Resize logo to 15% of image width
        logo_width = int(img.width * 0.15)
        logo_ratio = logo_width / logo.width
        logo_height = int(logo.height * logo_ratio)
        logo = logo.resize((logo_width, logo_height), Image.LANCZOS)

        # Position: bottom-right with padding
        padding = int(img.width * 0.03)
        x = img.width - logo_width - padding
        y = img.height - logo_height - padding

        # Paste with transparency
        img.paste(logo, (x, y), logo)

        # Save as RGB (for Meta upload compatibility)
        output = img.convert("RGB")
        output.save(image_path, "PNG")
        logger.info(f"Logo added to {image_path}")
        return image_path

    except ImportError:
        logger.warning("Pillow not installed — skipping logo overlay. Run: pip install Pillow")
        return image_path
    except Exception as e:
        logger.error(f"Logo overlay error: {e}")
        return image_path


def generate_image_higgsfield(prompt: str, index: int = 0) -> str | None:
    """Generate an image using Higgsfield AI API. Returns file path or None."""
    if not HIGGSFIELD_API_KEY:
        return None

    try:
        import time as _time

        # Step 1: Submit generation request
        resp = requests.post(
            "https://api.higgsfield.ai/v1/generations",
            headers={
                "Authorization": f"Bearer {HIGGSFIELD_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "task": "text-to-image",
                "model": "flux",
                "prompt": prompt,
                "width": 1080,
                "height": 1080,
                "steps": 30,
            },
            timeout=30,
        )
        data = resp.json()
        gen_id = data.get("id") or data.get("generation_id")
        if not gen_id:
            # If response contains direct URL or b64
            output_url = data.get("output", {}).get("url") or data.get("url")
            if output_url:
                return _download_image(output_url, index)
            logger.error(f"Higgsfield: no generation ID returned: {data}")
            return None

        # Step 2: Poll for completion (max 90 seconds)
        for _ in range(30):
            _time.sleep(3)
            status_resp = requests.get(
                f"https://api.higgsfield.ai/v1/generations/{gen_id}",
                headers={"Authorization": f"Bearer {HIGGSFIELD_API_KEY}"},
                timeout=15,
            )
            status_data = status_resp.json()
            status = status_data.get("status", "")

            if status == "completed":
                output_url = (status_data.get("output", {}).get("url")
                              or status_data.get("url")
                              or status_data.get("result", {}).get("url"))
                if output_url:
                    return _download_image(output_url, index)
                logger.error(f"Higgsfield completed but no URL: {status_data}")
                return None
            elif status in ("failed", "error", "cancelled"):
                logger.error(f"Higgsfield generation failed: {status_data}")
                return None

        logger.error("Higgsfield: generation timed out after 90s")
        return None

    except Exception as e:
        logger.error(f"Higgsfield error: {e}")
        return None


def generate_video_higgsfield(prompt: str, image_path: str = None) -> str | None:
    """Generate a video using Higgsfield AI. Returns file path or None."""
    if not HIGGSFIELD_API_KEY:
        return None

    try:
        import time as _time

        payload = {
            "task": "image-to-video" if image_path else "text-to-video",
            "prompt": prompt,
            "duration": 5,
            "fps": 30,
            "motion_intensity": "medium",
        }
        if image_path:
            # For image-to-video, we'd need to upload or provide URL
            # For now, use text-to-video as fallback
            payload["task"] = "text-to-video"

        resp = requests.post(
            "https://api.higgsfield.ai/v1/generations",
            headers={
                "Authorization": f"Bearer {HIGGSFIELD_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        data = resp.json()
        gen_id = data.get("id") or data.get("generation_id")
        if not gen_id:
            logger.error(f"Higgsfield video: no gen ID: {data}")
            return None

        # Poll for completion (videos take longer — max 3 min)
        for _ in range(60):
            _time.sleep(3)
            status_resp = requests.get(
                f"https://api.higgsfield.ai/v1/generations/{gen_id}",
                headers={"Authorization": f"Bearer {HIGGSFIELD_API_KEY}"},
                timeout=15,
            )
            status_data = status_resp.json()
            status = status_data.get("status", "")

            if status == "completed":
                output_url = (status_data.get("output", {}).get("url")
                              or status_data.get("url")
                              or status_data.get("result", {}).get("url"))
                if output_url:
                    return _download_video(output_url)
                return None
            elif status in ("failed", "error", "cancelled"):
                logger.error(f"Higgsfield video failed: {status_data}")
                return None

        logger.error("Higgsfield video: timed out")
        return None

    except Exception as e:
        logger.error(f"Higgsfield video error: {e}")
        return None


def _download_image(url: str, index: int) -> str | None:
    """Download an image from URL and save locally."""
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            tmp_dir = os.path.join(os.path.dirname(__file__), "tmp_images")
            os.makedirs(tmp_dir, exist_ok=True)
            filepath = os.path.join(tmp_dir, f"ad_image_{index}_{datetime.now().strftime('%Y%m%d%H%M%S')}.png")
            with open(filepath, "wb") as f:
                f.write(resp.content)
            # Add logo overlay
            filepath = add_logo_to_image(filepath)
            logger.info(f"Image downloaded: {filepath}")
            return filepath
    except Exception as e:
        logger.error(f"Image download error: {e}")
    return None


def _download_video(url: str) -> str | None:
    """Download a video from URL and save locally."""
    try:
        resp = requests.get(url, timeout=60)
        if resp.status_code == 200:
            tmp_dir = os.path.join(os.path.dirname(__file__), "tmp_images")
            os.makedirs(tmp_dir, exist_ok=True)
            filepath = os.path.join(tmp_dir, f"ad_video_{datetime.now().strftime('%Y%m%d%H%M%S')}.mp4")
            with open(filepath, "wb") as f:
                f.write(resp.content)
            logger.info(f"Video downloaded: {filepath}")
            return filepath
    except Exception as e:
        logger.error(f"Video download error: {e}")
    return None


def generate_image_together(prompt: str, index: int = 0) -> str | None:
    """Fallback: Generate image using Together AI FLUX model."""
    if not TOGETHER_API_KEY:
        return None

    try:
        import base64
        resp = requests.post(
            "https://api.together.xyz/v1/images/generations",
            headers={
                "Authorization": f"Bearer {TOGETHER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "black-forest-labs/FLUX.1-schnell-Free",
                "prompt": prompt,
                "width": 1080,
                "height": 1080,
                "steps": 4,
                "n": 1,
                "response_format": "b64_json",
            },
            timeout=60,
        )
        data = resp.json()

        if "data" in data and len(data["data"]) > 0:
            img_b64 = data["data"][0].get("b64_json", "")
            if img_b64:
                img_bytes = base64.b64decode(img_b64)
                tmp_dir = os.path.join(os.path.dirname(__file__), "tmp_images")
                os.makedirs(tmp_dir, exist_ok=True)
                filepath = os.path.join(tmp_dir, f"ad_image_{index}_{datetime.now().strftime('%Y%m%d%H%M%S')}.png")
                with open(filepath, "wb") as f:
                    f.write(img_bytes)
                filepath = add_logo_to_image(filepath)
                logger.info(f"Image generated (Together): {filepath}")
                return filepath
        logger.error(f"Together image failed: {data}")
        return None

    except Exception as e:
        logger.error(f"Together image error: {e}")
        return None


def generate_image_auto(prompt: str, index: int = 0) -> str | None:
    """Auto-select image generator: Higgsfield first, Together AI fallback."""
    # Try Higgsfield first
    result = generate_image_higgsfield(prompt, index)
    if result:
        return result
    # Fallback to Together AI
    result = generate_image_together(prompt, index)
    if result:
        return result
    logger.warning("No image generator available")
    return None


def generate_multiple_images(prompts: list) -> list:
    """Generate multiple images from a list of prompts. Returns list of file paths."""
    results = []
    for i, prompt in enumerate(prompts):
        path = generate_image_auto(prompt, i)
        results.append(path)
    return results


# Store generated images for each user
pending_images = {}


class MetaAdsExecutor:
    """Creates campaigns via Meta Marketing API."""

    def __init__(self, ad_account_id=None):
        self.token = META_ACCESS_TOKEN
        self.account_id = ad_account_id or AD_ACCOUNTS[DEFAULT_AD_ACCOUNT]["id"]
        self.page_id = META_PAGE_ID
        self.ig_actor_id = META_IG_ACTOR_ID
        self.base_url = "https://graph.facebook.com/v21.0"

    def _post(self, endpoint, data):
        data["access_token"] = self.token
        # Force lowest cost bid strategy (no bid cap needed) and remove bid_amount/bid_cap
        if "campaigns" in endpoint or "adsets" in endpoint:
            data["bid_strategy"] = "LOWEST_COST_WITHOUT_CAP"
            data.pop("bid_amount", None)
            data.pop("bid_cap", None)
        # Convert Python booleans to lowercase strings for form encoding
        for key, value in list(data.items()):
            if isinstance(value, bool):
                data[key] = "true" if value else "false"
        logger.info(f"POST {endpoint} | data keys: {list(data.keys())}")
        logger.info(f"POST {endpoint} | data values: { {k: v for k, v in data.items() if k != 'access_token'} }")
        resp = requests.post(f"{self.base_url}/{endpoint}", data=data, timeout=30)
        result = resp.json()
        if "error" in result:
            err = result["error"]
            detail = err.get("message", "Unknown")
            subcode = err.get("error_subcode", "")
            user_msg = err.get("error_user_msg", "")
            full_error = f"Meta API Error [{err.get('code', '?')}]: {detail}"
            if subcode:
                full_error += f" (subcode: {subcode})"
            if user_msg:
                full_error += f" — {user_msg}"
            logger.error(f"META API FULL RESPONSE: {json.dumps(result, indent=2)}")
            raise Exception(full_error)
        return result

    def _get(self, endpoint, params=None):
        if params is None:
            params = {}
        params["access_token"] = self.token
        resp = requests.get(f"{self.base_url}/{endpoint}", params=params, timeout=30)
        return resp.json()

    def auto_detect_page_id(self):
        """Auto-detect Page ID if not configured."""
        if self.page_id:
            return self.page_id
        logger.info("No PAGE_ID configured, auto-detecting...")
        pages = self._get("me/accounts", {"fields": "id,name"})
        if pages and pages.get("data") and len(pages["data"]) > 0:
            self.page_id = pages["data"][0]["id"]
            logger.info(f"Auto-detected Page: {pages['data'][0].get('name')} (ID: {self.page_id})")
            return self.page_id
        return None

    def upload_image(self, image_path: str) -> str | None:
        """Upload an image to Meta and return the image hash."""
        try:
            with open(image_path, "rb") as img_file:
                resp = requests.post(
                    f"{self.base_url}/{self.account_id}/adimages",
                    data={"access_token": self.token},
                    files={"filename": img_file},
                    timeout=60,
                )
            result = resp.json()
            if "images" in result:
                # The response has format: {"images": {"filename.png": {"hash": "xxx"}}}
                for key, val in result["images"].items():
                    image_hash = val.get("hash")
                    logger.info(f"Image uploaded to Meta: hash={image_hash}")
                    return image_hash
            else:
                logger.error(f"Image upload failed: {result}")
                return None
        except Exception as e:
            logger.error(f"Image upload error: {e}")
            return None

    def auto_detect_ig_id(self):
        """Auto-detect Instagram Business Account ID."""
        if self.ig_actor_id:
            return self.ig_actor_id
        if not self.page_id:
            return None
        ig = self._get(f"{self.page_id}", {"fields": "instagram_business_account"})
        if ig and ig.get("instagram_business_account"):
            self.ig_actor_id = ig["instagram_business_account"]["id"]
            logger.info(f"Auto-detected Instagram: ID {self.ig_actor_id}")
        return self.ig_actor_id

    def search_location(self, query):
        """Search for a city. Returns location dict or None."""
        try:
            result = self._get("search", {"type": "adgeolocation", "location_types": '["city"]', "q": query})
            if result.get("data") and len(result["data"]) > 0:
                loc = result["data"][0]
                logger.info(f"Location found: {query} → key={loc['key']}")
                return {"key": str(loc["key"]), "name": loc["name"], "radius": 0, "distance_unit": "kilometer"}
        except Exception as e:
            logger.warning(f"Location search failed for '{query}': {e}")
        return None

    def search_interest(self, query):
        """Search for a targeting interest. Returns interest dict or None."""
        try:
            result = self._get("search", {"type": "adinterest", "q": query})
            if result.get("data") and len(result["data"]) > 0:
                interest = result["data"][0]
                logger.info(f"Interest found: {query} → id={interest['id']}")
                return {"id": str(interest["id"]), "name": interest["name"]}
        except Exception as e:
            logger.warning(f"Interest search failed for '{query}': {e}")
        return None

    def create_full_campaign(self, campaign: dict) -> dict:
        """Create the complete campaign structure. Returns IDs."""
        results = {"success": False, "errors": [], "warnings": []}

        try:
            # ── VALIDATION ──
            # Auto-detect page ID if missing
            self.auto_detect_page_id()
            self.auto_detect_ig_id()

            if not self.page_id:
                raise Exception(
                    "Facebook Page ID not found. Please add META_PAGE_ID to your Railway variables. "
                    "Find it at: facebook.com/your_page → About → scroll to bottom → Page ID"
                )

            logger.info(f"Using Page ID: {self.page_id}")
            logger.info(f"Using IG Actor: {self.ig_actor_id or 'None'}")
            logger.info(f"Using Ad Account: {self.account_id}")

            # ── 1. CREATE CAMPAIGN ──
            budget_type = campaign.get("_budget_type", "ABO")
            adset = campaign.get("adset", {})
            daily_budget = adset.get("daily_budget", 200000)
            daily_budget = str(int(float(str(daily_budget).replace(",", "").replace(".", ""))))

            logger.info(f"Step 1: Creating campaign... (Budget type: {budget_type})")
            campaign_data = {
                "name": campaign["campaign_name"],
                "objective": campaign.get("objective", "OUTCOME_TRAFFIC"),
                "status": "PAUSED",
                "special_ad_categories": json.dumps([]),
            }

            if budget_type == "CBO":
                # CBO: budget at campaign level, Meta distributes across ad sets
                campaign_data["daily_budget"] = daily_budget
                campaign_data["is_campaign_budget_optimization_on"] = "true"
                logger.info(f"CBO mode: daily_budget {daily_budget} set on campaign")
            else:
                # ABO: explicitly tell campaign that CBO is OFF
                campaign_data["is_campaign_budget_optimization_on"] = "false"
                logger.info("ABO mode: CBO disabled on campaign, budget will be on ad set")

            camp_result = self._post(f"{self.account_id}/campaigns", campaign_data)
            campaign_id = camp_result["id"]
            results["campaign_id"] = campaign_id
            results["budget_type"] = budget_type
            logger.info(f"Campaign created: {campaign_id}")

            # ── 2. BUILD TARGETING ──
            logger.info("Step 2: Building targeting...")
            adset = campaign.get("adset", {})
            targeting = {
                "age_min": int(adset.get("age_min", 20)),
                "age_max": int(adset.get("age_max", 35)),
            }

            # Gender
            gender_map = {"women": [2], "female": [2], "men": [1], "male": [1]}
            genders = gender_map.get(str(adset.get("gender", "")).lower(), [])
            if genders:
                targeting["genders"] = genders

            # Resolve locations
            cities = []
            for loc in adset.get("locations", []):
                loc_name = loc if isinstance(loc, str) else loc.get("name", "")
                if not loc_name:
                    continue
                resolved = self.search_location(loc_name)
                if resolved:
                    cities.append(resolved)
                else:
                    results["warnings"].append(f"Location not found: {loc_name}")

            if cities:
                targeting["geo_locations"] = {"cities": cities}
            else:
                # Fallback to Malaysia if no cities found
                logger.warning("No cities found, falling back to Malaysia country targeting")
                targeting["geo_locations"] = {"countries": ["MY"]}
                results["warnings"].append("Cities not found, used Malaysia-wide targeting instead")

            # Resolve interests (optional - campaign works without them)
            interests = []
            for interest in adset.get("interests", []):
                interest_name = interest if isinstance(interest, str) else interest.get("name", "")
                if not interest_name:
                    continue
                resolved = self.search_interest(interest_name)
                if resolved:
                    interests.append(resolved)

            if interests:
                targeting["flexible_spec"] = [{"interests": interests}]
            else:
                results["warnings"].append("No interests could be resolved, using broad targeting")

            # Set language/locale targeting based on ad variant languages
            LOCALE_MAP = {
                "en": 6, "english": 6,
                "ms": 41, "malay": 41, "bahasa malaysia": 41, "bm": 41,
                "zh_cn": 44, "chinese": 44, "cn": 44, "mandarin": 44,
                "zh_tw": 45, "traditional chinese": 45,
                "ta": 56, "tamil": 56,
                "ar": 28, "arabic": 28,
                "ko": 25, "korean": 25,
                "ja": 9, "japanese": 9,
                "th": 57, "thai": 57,
                "id": 23, "indonesian": 23,
                "hi": 17, "hindi": 17,
            }
            # Get locales from adset.languages (set by Claude) or from ad_variants
            locale_ids = []
            adset_languages = adset.get("languages", [])
            if adset_languages:
                for lang_code in adset_languages:
                    lid = LOCALE_MAP.get(lang_code.lower().replace("-", "_"))
                    if lid and lid not in locale_ids:
                        locale_ids.append(lid)
            else:
                # Fallback: extract from ad variants
                for v in campaign.get("ad_variants", []):
                    lang_name = v.get("language", "").lower()
                    for key, lid in LOCALE_MAP.items():
                        if key in lang_name and lid not in locale_ids:
                            locale_ids.append(lid)

            if locale_ids:
                targeting["locales"] = locale_ids
                logger.info(f"Locale targeting set: {locale_ids}")
            else:
                logger.info("No locale targeting — all languages")

            # Set Advantage+ audience based on user's choice
            audience_type = campaign.get("_audience_type", "MANUAL")
            if audience_type == "ADV+":
                targeting["targeting_automation"] = {"advantage_audience": 1}
                logger.info("Advantage+ Audience ENABLED — Meta AI will expand targeting")
            else:
                targeting["targeting_automation"] = {"advantage_audience": 0}
                logger.info("Manual Targeting — using exact targeting specified")

            logger.info(f"Targeting built: {json.dumps(targeting)[:200]}")

            # ── 3. CREATE AD SET ──
            logger.info(f"Step 3: Creating ad set... (Budget type: {budget_type})")

            # Map objective to the correct optimization_goal, destination_type, and promoted_object
            objective = campaign.get("objective", "OUTCOME_TRAFFIC").upper()

            OBJECTIVE_CONFIG = {
                "OUTCOME_TRAFFIC": {
                    "optimization_goal": "LINK_CLICKS",
                    "destination_type": "WEBSITE",
                },
                "OUTCOME_SALES": {
                    "optimization_goal": "LINK_CLICKS",
                    "destination_type": "WEBSITE",
                },
                "OUTCOME_AWARENESS": {
                    "optimization_goal": "REACH",
                },
                "OUTCOME_LEADS": {
                    "optimization_goal": "QUALITY_LEAD",
                    "promoted_object": {"page_id": self.page_id},
                },
                "OUTCOME_ENGAGEMENT": {
                    "optimization_goal": "LINK_CLICKS",
                    "promoted_object": {"page_id": self.page_id},
                },
            }

            config = OBJECTIVE_CONFIG.get(objective, OBJECTIVE_CONFIG["OUTCOME_TRAFFIC"])
            opt_goal = config["optimization_goal"]
            logger.info(f"Objective: {objective} → optimization_goal: {opt_goal}")

            adset_data = {
                "name": adset.get("name", f"AdSet_{datetime.now().strftime('%Y%m%d')}"),
                "campaign_id": campaign_id,
                "billing_event": "IMPRESSIONS",
                "optimization_goal": opt_goal,
                "targeting": json.dumps(targeting),
                "status": "PAUSED",
            }

            if config.get("destination_type"):
                adset_data["destination_type"] = config["destination_type"]
            if config.get("promoted_object"):
                adset_data["promoted_object"] = json.dumps(config["promoted_object"])

            # Store debug info for error reporting
            results["debug_optimization_goal"] = opt_goal
            results["debug_destination_type"] = config.get("destination_type", "none")

            # ALWAYS set this field — Meta requires it on every ad set
            adset_data["is_adset_budget_sharing_enabled"] = "false"

            if budget_type == "CBO":
                logger.info("CBO mode: no budget on ad set (campaign controls budget)")
            else:
                adset_data["daily_budget"] = daily_budget
                logger.info(f"ABO mode: daily_budget {daily_budget} set on ad set")

            adset_result = self._post(f"{self.account_id}/adsets", adset_data)
            adset_id = adset_result["id"]
            results["adset_id"] = adset_id
            logger.info(f"Ad Set created: {adset_id}")

            # ── 4. CREATE AD CREATIVES + ADS ──
            logger.info("Step 4: Creating ad creatives...")
            results["ad_ids"] = []
            results["creative_ids"] = []

            website_url = campaign.get("website_url", "https://lovemaya.co")

            for variant in campaign.get("ad_variants", []):
                variant_name = variant.get("name", "Ad")
                try:
                    # Build the object_story_spec
                    link_data = {
                        "message": variant.get("primary_text", ""),
                        "link": website_url,
                        "name": variant.get("headline", ""),
                        "description": variant.get("description", ""),
                        "call_to_action": {
                            "type": variant.get("cta", "SHOP_NOW").upper(),
                            "value": {"link": website_url}
                        }
                    }

                    # Attach image if uploaded
                    img_hash = campaign.get("_image_hash")
                    if img_hash:
                        link_data["image_hash"] = img_hash

                    object_story_spec = {
                        "page_id": self.page_id,
                        "link_data": link_data,
                    }

                    # Try with Instagram first, retry without if it fails
                    creative_id = None
                    if self.ig_actor_id:
                        try:
                            object_story_spec["instagram_actor_id"] = self.ig_actor_id
                            creative_data = {
                                "name": variant_name,
                                "object_story_spec": json.dumps(object_story_spec),
                            }
                            logger.info(f"Creating creative with IG: {variant_name}")
                            creative_result = self._post(f"{self.account_id}/adcreatives", creative_data)
                            creative_id = creative_result["id"]
                        except Exception as ig_err:
                            logger.warning(f"IG actor failed ({ig_err}), retrying without Instagram...")
                            object_story_spec.pop("instagram_actor_id", None)
                            self.ig_actor_id = ""  # Don't try IG for remaining variants

                    if not creative_id:
                        # Create without Instagram (Facebook only)
                        creative_data = {
                            "name": variant_name,
                            "object_story_spec": json.dumps(object_story_spec),
                        }
                        logger.info(f"Creating creative (FB only): {variant_name}")
                        creative_result = self._post(f"{self.account_id}/adcreatives", creative_data)
                        creative_id = creative_result["id"]

                    results["creative_ids"].append(creative_id)

                    logger.info(f"Creating ad for creative {creative_id}")
                    ad_result = self._post(f"{self.account_id}/ads", {
                        "name": variant_name,
                        "adset_id": adset_id,
                        "creative": json.dumps({"creative_id": creative_id}),
                        "status": "PAUSED",
                    })
                    results["ad_ids"].append(ad_result["id"])
                    logger.info(f"Ad created: {ad_result['id']}")

                except Exception as e:
                    error_msg = f"Ad '{variant_name}': {str(e)}"
                    results["errors"].append(error_msg)
                    logger.error(f"Error creating ad variant: {e}")

            # Consider success if at least campaign + adset were created
            results["success"] = True
            if results["errors"]:
                results["success_note"] = "Campaign and ad set created, but some ads failed (see errors)"

        except Exception as e:
            results["errors"].append(str(e))
            logger.error(f"Campaign creation failed: {e}")

        return results


# ─────────────────────────────────────────────
# MANUS AI — BROWSER EXECUTION (OPTIONAL)
# ─────────────────────────────────────────────

def trigger_manus(instructions: str) -> dict:
    """
    Trigger Manus AI to execute campaign creation in browser.
    This is an alternative to the Meta API approach.

    Note: Manus API availability depends on your plan.
    If no API key, the bot sends you the instructions to paste manually.
    """
    if not MANUS_API_KEY:
        return {"method": "manual", "instructions": instructions}

    try:
        resp = requests.post(
            "https://api.manus.im/v1/tasks",
            headers={
                "Authorization": f"Bearer {MANUS_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "prompt": instructions,
                "mode": "auto",
            },
            timeout=60,
        )
        result = resp.json()
        return {"method": "api", "task_id": result.get("id"), "status": result.get("status")}
    except Exception as e:
        logger.error(f"Manus trigger failed: {e}")
        return {"method": "failed", "error": str(e), "instructions": instructions}


# ─────────────────────────────────────────────
# TELEGRAM HANDLERS
# ─────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message."""
    user = update.effective_user
    await update.message.reply_text(
        f"Hey {user.first_name}! 👋\n\n"
        f"I'm the Lovemaya Ads Engine. Send me a brief and I'll create a full Meta campaign.\n\n"
        f"Example:\n"
        f"\"Bath gel, MYR10/day, women 18-45, Malaysia, traffic, shopify, in EN BM CN\"\n\n"
        f"📂 Ad Accounts:\n"
        f"• \"cpas sg\" → CPAS Singapore\n"
        f"• \"cpas my\" → CPAS Malaysia\n"
        f"• \"shopify\" → Shopify Malaysia (default)\n\n"
        f"💰 Budget Types:\n"
        f"• \"CBO\" → Campaign budget (shared)\n"
        f"• \"ABO\" → Ad set budget (default)\n\n"
        f"Commands:\n"
        f"/start — This message\n"
        f"/status [campaign_id] — Check campaign status\n"
        f"/help — Tips for writing briefs"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help with writing briefs."""
    await update.message.reply_text(
        "📝 How to write a good brief:\n\n"
        "Include any of these (I'll fill in defaults for the rest):\n"
        "• Product name\n"
        "• Daily budget (e.g. MYR10, MYR30)\n"
        "• Target audience (age, gender)\n"
        "• Locations (cities/country)\n"
        "• Goal (traffic, sales, awareness, leads)\n"
        "• Languages (english, malay, chinese, tamil, etc.)\n"
        "• Budget type (CBO or ABO)\n"
        "• Audience type (adv+ or manual targeting)\n"
        "• Ad account (cpas sg, cpas my, shopify)\n"
        "• Interests (e.g. interest: skincare, perfume, K-beauty)\n"
        "• Promo/offer details\n\n"
        "🌐 LANGUAGES:\n"
        "Just mention the languages you want!\n"
        "• No language mentioned → defaults to EN, BM, CN\n"
        "• \"in english and chinese\" → 2 variants\n"
        "• \"EN BM CN Tamil\" → 4 variants\n\n"
        "💡 STRATEGY COMMANDS:\n"
        "/ideas [product] — Get 5 campaign ideas based on DTC trends\n"
        "/learn [brand] — Study a competitor's ad strategy\n"
        "/funnel [product] [budget] — Full-funnel campaign plan\n\n"
        "🧠 LEARNING COMMANDS:\n"
        "/feedback [text] — Teach me your preferences\n"
        "/memory — See what I've learned\n"
        "/forget — Clear all memories\n\n"
        "Example briefs:\n"
        "\"Bath gel, MYR10/day, women 18-45, Malaysia, traffic, shopify, ABO, in EN and BM, interest: skincare, body care\"\n\n"
        "\"Perfume launch, MYR30/day, KL Penang, awareness, cpas my, CBO, adv+, in EN BM CN Tamil\"\n\n"
        "\"Body scrub, SGD5/day, Singapore, sales, cpas sg, chinese only, interest: beauty, fragrance\""
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check campaign delivery status."""
    if not META_ACCESS_TOKEN:
        await update.message.reply_text("Meta API not configured. Add META_ACCESS_TOKEN to .env")
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /status <campaign_id>")
        return

    campaign_id = args[0]
    executor = MetaAdsExecutor()
    try:
        result = executor._get(campaign_id, {"fields": "name,status,effective_status"})
        await update.message.reply_text(
            f"📊 Campaign: {result.get('name', 'N/A')}\n"
            f"Status: {result.get('effective_status', 'Unknown')}"
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_ideas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate campaign ideas based on DTC knowledge and product."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Sorry, you're not authorized.")
        return

    product_text = " ".join(context.args) if context.args else ""
    if not product_text:
        await update.message.reply_text(
            "💡 Tell me a product and I'll suggest campaign ideas!\n\n"
            "Examples:\n"
            "/ideas bath gel\n"
            "/ideas body mist jasmine\n"
            "/ideas lotion gift set\n"
            "/ideas body scrub for ramadan"
        )
        return

    await update.message.reply_text("🧠 Analyzing DTC trends and generating ideas...")

    try:
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        today = datetime.now().strftime("%B %Y")

        ideas_prompt = f"""You are a DTC personal care marketing strategist. Based on your knowledge of successful brands like Sol de Janeiro, Glossier, CeraVe, Dr. Squatch, and Native, suggest 5 campaign ideas for Lovemaya.

{DTC_KNOWLEDGE}

{get_memory_prompt()}

Product: {product_text}
Current month: {today}
Brand: Lovemaya (Malaysian body care brand — bath gel, lotion, body mist, scrub)
Market: Malaysia & Singapore

For each idea provide:
1. Campaign Name — catchy and specific
2. Angle — which proven angle to use (benefit-led, sensory, social proof, etc.)
3. Objective — awareness, traffic, or sales
4. Hook — the first line of ad copy (attention-grabbing)
5. Why It Works — 1 sentence explaining the DTC strategy behind it
6. Suggested Budget — daily budget in MYR

Keep it practical and actionable. Format clearly with numbers and emojis."""

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2048,
            messages=[{"role": "user", "content": ideas_prompt}]
        )

        ideas = message.content[0].text.strip()
        # Split into chunks if too long for Telegram (4096 char limit)
        if len(ideas) > 4000:
            parts = [ideas[i:i+4000] for i in range(0, len(ideas), 4000)]
            for part in parts:
                await update.message.reply_text(part)
        else:
            await update.message.reply_text(f"💡 Campaign Ideas for {product_text}:\n\n{ideas}")

    except Exception as e:
        await update.message.reply_text(f"Error generating ideas: {e}")


async def cmd_learn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Research a competitor brand's ad strategy."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Sorry, you're not authorized.")
        return

    brand_text = " ".join(context.args) if context.args else ""
    if not brand_text:
        await update.message.reply_text(
            "🔍 Tell me a brand to study!\n\n"
            "Examples:\n"
            "/learn Sol de Janeiro\n"
            "/learn Glossier\n"
            "/learn CeraVe\n"
            "/learn Dr. Squatch\n"
            "/learn any Malaysian body care brand"
        )
        return

    await update.message.reply_text(f"🔍 Studying {brand_text}'s ad strategy...")

    try:
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

        learn_prompt = f"""You are a DTC advertising analyst. Research and analyze the Meta/Instagram advertising strategy of: {brand_text}

Focus on:
1. Their brand positioning and unique selling points
2. Ad copy angles they commonly use
3. Visual style and creative formats
4. Target audience and messaging approach
5. What Lovemaya (Malaysian body care: bath gel, lotion, body mist, scrub) can LEARN and ADAPT from this brand
6. Specific actionable takeaways — ad copy examples, angles to test, creative ideas

{DTC_KNOWLEDGE}

Be specific and practical. Give examples of ad copy hooks that Lovemaya could adapt. Format with emojis for readability."""

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2048,
            messages=[{"role": "user", "content": learn_prompt}]
        )

        analysis = message.content[0].text.strip()
        if len(analysis) > 4000:
            parts = [analysis[i:i+4000] for i in range(0, len(analysis), 4000)]
            for part in parts:
                await update.message.reply_text(part)
        else:
            await update.message.reply_text(f"🔍 Brand Analysis: {brand_text}\n\n{analysis}")

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_funnel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Suggest a full-funnel campaign structure for a product."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Sorry, you're not authorized.")
        return

    product_text = " ".join(context.args) if context.args else ""
    if not product_text:
        await update.message.reply_text(
            "🔻 Tell me a product and total budget, I'll plan your full funnel!\n\n"
            "Examples:\n"
            "/funnel bath gel MYR50/day\n"
            "/funnel body mist collection MYR100/day\n"
            "/funnel new product launch MYR30/day"
        )
        return

    await update.message.reply_text("🔻 Building your full-funnel strategy...")

    try:
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        today = datetime.now().strftime("%B %Y")

        funnel_prompt = f"""You are a DTC performance marketing strategist. Create a full-funnel Meta Ads campaign structure for Lovemaya.

{DTC_KNOWLEDGE}

{get_memory_prompt()}

Product/Brief: {product_text}
Current month: {today}
Brand: Lovemaya (Malaysian body care)

Create a 3-tier funnel with:

🔹 TOP OF FUNNEL (Awareness):
- Campaign objective, optimization goal
- Budget allocation (% of total)
- Ad format recommendation
- 2 ad copy hooks
- Targeting approach

🔹 MID FUNNEL (Consideration):
- Campaign objective, optimization goal
- Budget allocation
- Ad format recommendation
- 2 ad copy hooks
- Targeting (retargeting strategy)

🔹 BOTTOM FUNNEL (Conversion):
- Campaign objective, optimization goal
- Budget allocation
- Ad format recommendation
- 2 ad copy hooks with offers
- Targeting (warm audiences)

Also suggest: timeline, KPIs to track, and when to scale.
Format clearly with emojis. Be specific with actual ad copy examples."""

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2048,
            messages=[{"role": "user", "content": funnel_prompt}]
        )

        funnel = message.content[0].text.strip()
        if len(funnel) > 4000:
            parts = [funnel[i:i+4000] for i in range(0, len(funnel), 4000)]
            for part in parts:
                await update.message.reply_text(part)
        else:
            await update.message.reply_text(f"🔻 Full Funnel Plan:\n\n{funnel}")

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save user feedback so the bot learns and improves."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Sorry, you're not authorized.")
        return

    feedback_text = " ".join(context.args) if context.args else ""
    if not feedback_text:
        await update.message.reply_text(
            "💡 Tell me what to improve! Examples:\n\n"
            "/feedback always use emotional angles for body mist\n"
            "/feedback don't use 'limited time' in headlines\n"
            "/feedback I prefer shorter primary text\n"
            "/feedback use more Malay slang in BM variants\n"
            "/feedback default interest should be skincare and fragrance"
        )
        return

    add_memory(feedback_text)
    memories = load_memory()
    await update.message.reply_text(
        f"✅ Got it! I'll remember that.\n\n"
        f"📝 \"{feedback_text}\"\n\n"
        f"🧠 Total memories: {len(memories)}\n"
        f"This will be applied to all future campaigns."
    )


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show what the bot has learned."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Sorry, you're not authorized.")
        return

    memories = load_memory()
    if not memories:
        await update.message.reply_text(
            "🧠 No memories yet!\n\n"
            "Use /feedback to teach me your preferences.\n"
            "Example: /feedback always use benefit-led angles for skincare"
        )
        return

    lines = []
    for i, m in enumerate(memories, 1):
        lines.append(f"{i}. {m['feedback']} ({m['date']})")

    await update.message.reply_text(
        f"🧠 My memories ({len(memories)} total):\n\n" +
        "\n".join(lines) +
        "\n\nUse /forget to clear all memories."
    )


async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear all learned memories."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Sorry, you're not authorized.")
        return

    save_memory([])
    await update.message.reply_text("🗑 All memories cleared. Starting fresh!")


async def handle_brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main handler — receives brief, generates campaign, asks for approval."""
    user_id = update.effective_user.id

    if not is_authorized(user_id):
        await update.message.reply_text("Sorry, you're not authorized to use this bot.")
        return

    brief_text = update.message.text
    logger.info(f"Brief received from {user_id}: {brief_text[:100]}...")

    # Step 1: Detect ad account, budget type, and audience type from brief
    detected_account = detect_ad_account(brief_text)
    budget_type = detect_budget_type(brief_text)
    audience_type = detect_audience_type(brief_text)

    # Step 2: Acknowledge
    budget_label = "Campaign Budget (CBO)" if budget_type == "CBO" else "Ad Set Budget (ABO)"
    audience_label = "Advantage+ Audience (AI)" if audience_type == "ADV+" else "Manual Targeting"
    status_msg = await update.message.reply_text(
        f"🧠 Generating campaign with Claude AI...\n"
        f"📂 Ad Account: {detected_account['name']}\n"
        f"💰 Budget Type: {budget_label}\n"
        f"👥 Audience: {audience_label}"
    )

    try:
        # Step 3: Generate campaign via Claude
        campaign = generate_campaign_with_claude(brief_text)
        logger.info(f"Campaign generated: {campaign.get('campaign_name')}")

        # Attach the detected ad account and budget type to the campaign
        account_key = campaign.get("ad_account", DEFAULT_AD_ACCOUNT)
        if account_key in AD_ACCOUNTS:
            campaign["_ad_account"] = AD_ACCOUNTS[account_key]
        else:
            campaign["_ad_account"] = detected_account
        campaign["_budget_type"] = budget_type
        campaign["_audience_type"] = audience_type
        logger.info(f"Using ad account: {campaign['_ad_account']['name']} ({campaign['_ad_account']['id']})")
        logger.info(f"Budget type: {budget_type}")
        logger.info(f"Audience type: {audience_type}")

        # Store for approval
        pending_campaigns[user_id] = campaign

        # Step 4: Send campaign preview
        variants_preview = ""
        for v in campaign.get("ad_variants", []):
            lang = v.get("language", "")
            lang_tag = f" ({lang})" if lang else ""
            variants_preview += f"\n• [{v.get('angle', '')}{lang_tag}] {v.get('primary_text', '')}"

        acct = campaign.get("_ad_account", detected_account)
        currency = acct.get("currency", "MYR")
        budget = campaign.get("adset", {}).get("daily_budget", "?")
        budget_label = "Campaign Budget (CBO)" if budget_type == "CBO" else "Ad Set Budget (ABO)"
        audience_label = "Advantage+ (AI)" if campaign.get("_audience_type") == "ADV+" else "Manual"

        preview_text = (
            f"✅ Campaign Ready!\n\n"
            f"📂 Ad Account: {acct['name']}\n"
            f"📋 {campaign.get('campaign_name', 'Campaign')}\n"
            f"🎯 {campaign.get('objective', 'TRAFFIC')}\n"
            f"💰 {currency} {budget} (cents)/day — {budget_label}\n"
            f"🧠 Audience: {audience_label}\n"
            f"👥 {campaign.get('adset', {}).get('gender', 'All')}, "
            f"age {campaign.get('adset', {}).get('age_min', 18)}-{campaign.get('adset', {}).get('age_max', 65)}\n"
            f"📍 {', '.join(str(l) for l in campaign.get('adset', {}).get('locations', []))}\n\n"
            f"📝 Ad Variants:{variants_preview}\n\n"
            f"🛡 Policy: {campaign.get('policy_check', 'No issues')}"
        )

        await status_msg.edit_text(preview_text)

        # Step 5: Generate AI images (if Together API is configured)
        image_prompts = campaign.get("image_prompts", [])
        # Fallback: old single image_prompt field
        if not image_prompts and campaign.get("image_prompt"):
            image_prompts = [campaign["image_prompt"]]

        if (HIGGSFIELD_API_KEY or TOGETHER_API_KEY) and image_prompts:
            await update.message.reply_text("🎨 Generating ad images... (this takes ~30 seconds)")

            generated_paths = generate_multiple_images(image_prompts[:3])
            valid_images = [(i, p) for i, p in enumerate(generated_paths) if p]

            if valid_images:
                # Store images for this user
                pending_images[user_id] = {
                    "paths": generated_paths,
                    "prompts": image_prompts,
                }

                # Send each image with a selection button
                for idx, path in valid_images:
                    try:
                        with open(path, "rb") as photo:
                            await update.message.reply_photo(
                                photo=photo,
                                caption=f"🖼 Option {idx + 1}: {image_prompts[idx][:150]}...",
                                reply_markup=InlineKeyboardMarkup([[
                                    InlineKeyboardButton(f"✅ Use Image {idx + 1}", callback_data=f"pick_img_{idx}"),
                                ]]),
                            )
                    except Exception as img_err:
                        logger.error(f"Failed to send image {idx}: {img_err}")

                # Also offer to skip image selection
                await update.message.reply_text(
                    "👆 Pick an image above, or choose an action:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🚀 Create without image", callback_data="exec_api")],
                        [InlineKeyboardButton("❌ Cancel", callback_data="exec_cancel")],
                    ]),
                )
            else:
                # Image generation failed — show normal buttons
                keyboard = [
                    [
                        InlineKeyboardButton("🚀 Create via API", callback_data="exec_api"),
                        InlineKeyboardButton("🤖 Send to Manus", callback_data="exec_manus"),
                    ],
                    [
                        InlineKeyboardButton("📋 Copy Instructions", callback_data="exec_copy"),
                        InlineKeyboardButton("❌ Cancel", callback_data="exec_cancel"),
                    ],
                ]
                await update.message.reply_text(
                    "⚠️ Image generation failed. You can still create the campaign:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
        else:
            # No Together API — show normal approval buttons
            keyboard = [
                [
                    InlineKeyboardButton("🚀 Create via API", callback_data="exec_api"),
                    InlineKeyboardButton("🤖 Send to Manus", callback_data="exec_manus"),
                ],
                [
                    InlineKeyboardButton("📋 Copy Instructions", callback_data="exec_copy"),
                    InlineKeyboardButton("❌ Cancel", callback_data="exec_cancel"),
                ],
            ]
            await update.message.reply_text(
                "What should I do?",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

    except json.JSONDecodeError as e:
        await status_msg.edit_text(f"⚠️ Claude returned invalid JSON. Try rephrasing your brief.\nError: {e}")
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {e}")
        logger.error(f"Campaign generation failed: {e}")


async def handle_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button clicks for campaign approval."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    action = query.data
    campaign = pending_campaigns.get(user_id)

    if not campaign and not action.startswith("pick_img_"):
        await query.edit_message_text("⚠️ No pending campaign found. Send a new brief.")
        return

    # ── PICK IMAGE ──
    if action.startswith("pick_img_"):
        img_index = int(action.split("_")[-1])
        user_images = pending_images.get(user_id, {})
        paths = user_images.get("paths", [])

        if img_index < len(paths) and paths[img_index]:
            # Store selected image path in campaign
            if campaign:
                campaign["_selected_image"] = paths[img_index]
                pending_campaigns[user_id] = campaign

            video_btn = []
            if HIGGSFIELD_API_KEY:
                video_btn = [InlineKeyboardButton("🎬 Generate Video from this Image", callback_data=f"gen_video_{img_index}")]

            await query.edit_message_text(
                f"✅ Image {img_index + 1} selected!\n\nNow choose:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🚀 Create Campaign with this Image", callback_data="exec_api_with_image")],
                    video_btn if video_btn else [],
                    [InlineKeyboardButton("❌ Cancel", callback_data="exec_cancel")],
                ]),
            )
        else:
            await query.edit_message_text("⚠️ Image not found. Try again or create without image.")
        return

    # ── GENERATE VIDEO FROM SELECTED IMAGE ──
    if action.startswith("gen_video_"):
        img_index = int(action.split("_")[-1])
        user_images = pending_images.get(user_id, {})
        paths = user_images.get("paths", [])
        prompts = user_images.get("prompts", [])

        if img_index < len(paths) and paths[img_index]:
            await query.edit_message_text("🎬 Generating video from your image... (this takes ~1-2 minutes)")

            video_prompt = prompts[img_index] if img_index < len(prompts) else "Animate this product image with gentle motion"
            video_path = generate_video_higgsfield(video_prompt, paths[img_index])

            if video_path:
                try:
                    with open(video_path, "rb") as video_file:
                        await query.message.reply_video(
                            video=video_file,
                            caption="🎬 Ad video generated! You can download and use this in Ads Manager.",
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("🚀 Create Campaign with Image", callback_data="exec_api_with_image")],
                                [InlineKeyboardButton("❌ Cancel", callback_data="exec_cancel")],
                            ]),
                        )
                except Exception as vid_err:
                    await query.message.reply_text(f"⚠️ Couldn't send video: {vid_err}")
            else:
                await query.message.reply_text(
                    "⚠️ Video generation failed. You can still create the campaign with the image:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🚀 Create Campaign with Image", callback_data="exec_api_with_image")],
                    ]),
                )
        return

    # ── EXECUTE VIA META API (with or without image) ──
    if action in ("exec_api", "exec_api_with_image"):
        if not META_ACCESS_TOKEN:
            await query.edit_message_text(
                "⚠️ Meta API not configured.\n"
                "Add META_ACCESS_TOKEN to your .env file.\n"
                "See SETUP_GUIDE.md for instructions."
            )
            return

        acct = campaign.get("_ad_account", AD_ACCOUNTS[DEFAULT_AD_ACCOUNT])
        selected_image = campaign.get("_selected_image") if action == "exec_api_with_image" else None

        status_text = f"⏳ Creating campaign via Meta API...\n📂 Account: {acct['name']}"
        if selected_image:
            status_text += "\n🖼 Uploading selected image..."
        await query.edit_message_text(status_text)

        executor = MetaAdsExecutor(ad_account_id=acct["id"])

        # Upload image to Meta if selected
        image_hash = None
        if selected_image:
            image_hash = executor.upload_image(selected_image)
            if image_hash:
                campaign["_image_hash"] = image_hash
                logger.info(f"Image uploaded to Meta: {image_hash}")
            else:
                logger.warning("Image upload failed — creating campaign without image")

        result = executor.create_full_campaign(campaign)

        if result["success"]:
            has_image = "🖼 Image attached!" if image_hash else "→ Upload ad images in Ads Manager"
            msg = (
                f"✅ Campaign created!\n\n"
                f"Campaign ID: {result.get('campaign_id', 'N/A')}\n"
                f"Ad Set ID: {result.get('adset_id', 'N/A')}\n"
                f"Ads created: {len(result.get('ad_ids', []))}\n\n"
                f"⚠️ Status: PAUSED\n"
                f"{has_image}\n"
                f"→ Review and activate in Ads Manager\n"
                f"→ Or use /status {result.get('campaign_id', '')} to check"
            )
            if result.get("warnings"):
                msg += f"\n\n⚠️ Notes:\n" + "\n".join(result["warnings"])
            if result.get("errors"):
                msg += f"\n\n⚠️ Some ads had issues:\n" + "\n".join(result["errors"])
        else:
            errors_text = "\n".join(result.get("errors", ["Unknown error"]))
            # Include debug info so we can see what was sent
            debug_obj = campaign.get("objective", "?")
            debug_opt = result.get("debug_optimization_goal", "?")
            debug_dest = result.get("debug_destination_type", "?")
            msg = (
                f"❌ Campaign creation failed ({BOT_VERSION}):\n\n"
                f"{errors_text}\n\n"
                f"🔍 Debug: obj={debug_obj} opt={debug_opt} dest={debug_dest}\n\n"
                f"💡 Common fixes:\n"
                f"• 'Invalid parameter' → Check META_PAGE_ID is correct\n"
                f"• 'Invalid token' → Refresh META_ACCESS_TOKEN\n"
                f"• 'Permission' → Token needs ads_management permission\n\n"
                f"Check Railway logs for full error details."
            )

        await query.edit_message_text(msg)

    # ── EXECUTE VIA MANUS ──
    elif action == "exec_manus":
        manus_instructions = campaign.get("manus_instructions", "No Manus instructions generated.")
        await query.edit_message_text("⏳ Sending to Manus AI...")

        result = trigger_manus(manus_instructions)

        if result["method"] == "api":
            await query.edit_message_text(
                f"✅ Manus task created!\n"
                f"Task ID: {result.get('task_id')}\n"
                f"Status: {result.get('status')}\n\n"
                f"Manus will create the campaign in Ads Manager."
            )
        elif result["method"] == "manual":
            # Send instructions in chunks (Telegram message limit is 4096 chars)
            await query.edit_message_text("📋 Manus API not configured. Sending instructions to copy:\n")
            instructions = result["instructions"]
            for i in range(0, len(instructions), 4000):
                chunk = instructions[i:i+4000]
                await query.message.reply_text(f"```\n{chunk}\n```", parse_mode="Markdown")
        else:
            await query.edit_message_text(
                f"❌ Manus failed: {result.get('error')}\n\n"
                f"Manual instructions sent below:"
            )
            await query.message.reply_text(result.get("instructions", "No instructions"))

    # ── COPY INSTRUCTIONS ──
    elif action == "exec_copy":
        manus_instructions = campaign.get("manus_instructions", "No instructions generated.")
        full_output = json.dumps(campaign, indent=2, ensure_ascii=False)

        await query.edit_message_text("📋 Full campaign JSON sent below. Copy and use anywhere:")

        # Send in chunks
        for i in range(0, len(full_output), 4000):
            chunk = full_output[i:i+4000]
            await query.message.reply_text(chunk)

    # ── CANCEL ──
    elif action == "exec_cancel":
        pending_campaigns.pop(user_id, None)
        await query.edit_message_text("❌ Campaign cancelled. Send a new brief whenever you're ready.")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    """Start the bot."""
    if not TELEGRAM_TOKEN:
        print("ERROR: Set TELEGRAM_BOT_TOKEN in .env")
        print("Get one from @BotFather on Telegram")
        return

    if not CLAUDE_API_KEY:
        print("ERROR: Set CLAUDE_API_KEY in .env")
        print("Get one from console.anthropic.com")
        return

    print("=" * 50)
    print("  LOVEMAYA ADS BOT — Starting")
    print(f"  Meta API: {'Configured' if META_ACCESS_TOKEN else 'Not configured (manual mode)'}")
    print(f"  Manus API: {'Configured' if MANUS_API_KEY else 'Not configured (copy mode)'}")
    print(f"  Allowed users: {ALLOWED_USER_IDS or 'All (no restriction)'}")
    print("=" * 50)

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("ideas", cmd_ideas))
    app.add_handler(CommandHandler("learn", cmd_learn))
    app.add_handler(CommandHandler("funnel", cmd_funnel))
    app.add_handler(CommandHandler("feedback", cmd_feedback))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CallbackQueryHandler(handle_approval))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_brief))

    print("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
