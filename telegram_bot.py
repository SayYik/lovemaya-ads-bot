#!/usr/bin/env python3
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
META_AD_ACCOUNT_ID = os.getenv("META_AD_ACCOUNT_ID", "act_752480016788280")
META_PAGE_ID = os.getenv("META_PAGE_ID", "")
META_IG_ACTOR_ID = os.getenv("META_IG_ACTOR_ID", "")
MANUS_API_KEY = os.getenv("MANUS_API_KEY", "")
ALLOWED_USER_IDS = [int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()]

# Store pending campaigns waiting for approval
pending_campaigns = {}

# ─────────────────────────────────────────────
# LOVEMAYA BRAND CONTEXT (sent to Claude)
# ─────────────────────────────────────────────

BRAND_SYSTEM_PROMPT = """You are the Meta Ads Engine for Lovemaya. When given a brief, generate a COMPLETE campaign in JSON format.

BRAND INFO:
- Business: Lovemaya
- Website: https://lovemaya.co
- Instagram: @lovemaya.my
- Industry: Beauty & personal care / body care
- Currency: IDR
- Languages: Bahasa Indonesia and English
- Tone: Elegant, fresh & natural, affordable luxury
- Ad Account: act_752480016788280

RESPOND WITH VALID JSON ONLY (no markdown, no ```). Use this exact structure:

{
  "campaign_name": "Lovemaya_[Product]_[Objective]_[MonthYear]",
  "objective": "OUTCOME_TRAFFIC",
  "currency": "IDR",
  "website_url": "https://lovemaya.co",
  "adset": {
    "name": "[descriptive ad set name]",
    "daily_budget": 200000,
    "age_min": 20,
    "age_max": 35,
    "gender": "women",
    "optimization_goal": "LINK_CLICKS",
    "locations": ["Jakarta, Indonesia"],
    "interests": ["Beauty", "Fragrance"]
  },
  "ad_variants": [
    {
      "name": "Variant_A_[Angle]",
      "primary_text": "under 125 chars",
      "headline": "under 40 chars",
      "description": "under 30 chars",
      "cta": "SHOP_NOW",
      "angle": "benefit-led"
    }
  ],
  "image_prompt": "A detailed Ideogram.ai prompt for the ad image...",
  "policy_check": "No policy issues found.",
  "manus_instructions": "Step-by-step instructions for Manus AI to create this in Meta Ads Manager...",
  "summary": "A short 2-3 line summary for the Telegram reply"
}

RULES:
- Always generate exactly 3 ad variants with different angles
- Primary text under 125 chars, headline under 40, description under 30
- CTA must be: SHOP_NOW, LEARN_MORE, SIGN_UP, BOOK_NOW, or GET_OFFER
- Include detailed Manus instructions with exact button clicks and field values
- Check against Meta Advertising Standards and flag any risks in policy_check
- Match the budget, targeting, and objective from the user's brief
- If the brief is missing info, use sensible Lovemaya defaults
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

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        system=BRAND_SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": f"Create a Meta ad campaign for this brief:\n\n{brief_text}"}
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

class MetaAdsExecutor:
    """Creates campaigns via Meta Marketing API."""

    def __init__(self):
        self.token = META_ACCESS_TOKEN
        self.account_id = META_AD_ACCOUNT_ID
        self.page_id = META_PAGE_ID
        self.ig_actor_id = META_IG_ACTOR_ID
        self.base_url = "https://graph.facebook.com/v21.0"

    def _post(self, endpoint, data):
        data["access_token"] = self.token
        resp = requests.post(f"{self.base_url}/{endpoint}", data=data, timeout=30)
        result = resp.json()
        if "error" in result:
            raise Exception(f"Meta API Error: {result['error'].get('message', 'Unknown')}")
        return result

    def _get(self, endpoint, params=None):
        if params is None:
            params = {}
        params["access_token"] = self.token
        resp = requests.get(f"{self.base_url}/{endpoint}", params=params, timeout=30)
        return resp.json()

    def search_location(self, query):
        result = self._get("search", {"type": "adgeolocation", "location_types": '["city"]', "q": query})
        if result.get("data"):
            loc = result["data"][0]
            return {"key": loc["key"], "name": loc["name"], "radius": 0, "distance_unit": "kilometer"}
        return None

    def search_interest(self, query):
        result = self._get("search", {"type": "adinterest", "q": query})
        if result.get("data"):
            return {"id": result["data"][0]["id"], "name": result["data"][0]["name"]}
        return None

    def create_full_campaign(self, campaign: dict) -> dict:
        """Create the complete campaign structure. Returns IDs."""
        results = {"success": False, "errors": []}

        try:
            # 1. Create Campaign
            camp_result = self._post(f"{self.account_id}/campaigns", {
                "name": campaign["campaign_name"],
                "objective": campaign.get("objective", "OUTCOME_TRAFFIC"),
                "status": "PAUSED",
                "special_ad_categories": "[]",
            })
            campaign_id = camp_result["id"]
            results["campaign_id"] = campaign_id
            logger.info(f"Campaign created: {campaign_id}")

            # 2. Build targeting
            adset = campaign.get("adset", {})
            targeting = {
                "age_min": adset.get("age_min", 20),
                "age_max": adset.get("age_max", 35),
            }

            gender_map = {"women": [2], "female": [2], "men": [1], "male": [1]}
            genders = gender_map.get(adset.get("gender", "").lower(), [])
            if genders:
                targeting["genders"] = genders

            # Resolve locations
            cities = []
            for loc in adset.get("locations", []):
                resolved = self.search_location(loc if isinstance(loc, str) else loc.get("name", ""))
                if resolved:
                    cities.append(resolved)
            if cities:
                targeting["geo_locations"] = {"cities": cities}

            # Resolve interests
            interests = []
            for interest in adset.get("interests", []):
                resolved = self.search_interest(interest if isinstance(interest, str) else interest.get("name", ""))
                if resolved:
                    interests.append(resolved)
            if interests:
                targeting["flexible_spec"] = [{"interests": interests}]

            # 3. Create Ad Set
            adset_data = {
                "name": adset.get("name", f"AdSet_{datetime.now().strftime('%Y%m%d')}"),
                "campaign_id": campaign_id,
                "daily_budget": str(adset.get("daily_budget", 200000)),
                "billing_event": "IMPRESSIONS",
                "optimization_goal": adset.get("optimization_goal", "LINK_CLICKS"),
                "targeting": json.dumps(targeting),
                "status": "PAUSED",
            }

            if campaign.get("objective", "").upper() in ("OUTCOME_TRAFFIC",):
                adset_data["destination_type"] = "WEBSITE"

            adset_result = self._post(f"{self.account_id}/adsets", adset_data)
            adset_id = adset_result["id"]
            results["adset_id"] = adset_id
            logger.info(f"Ad Set created: {adset_id}")

            # 4. Create Ads for each variant (without image — user uploads later)
            results["ad_ids"] = []
            results["creative_ids"] = []

            for variant in campaign.get("ad_variants", []):
                try:
                    website_url = campaign.get("website_url", "https://lovemaya.co")
                    creative_data = {
                        "name": variant.get("name", "Creative"),
                        "object_story_spec": json.dumps({
                            "page_id": self.page_id,
                            "link_data": {
                                "message": variant.get("primary_text", ""),
                                "link": website_url,
                                "name": variant.get("headline", ""),
                                "description": variant.get("description", ""),
                                "call_to_action": {
                                    "type": variant.get("cta", "SHOP_NOW"),
                                    "value": {"link": website_url}
                                }
                            }
                        }),
                    }
                    creative_result = self._post(f"{self.account_id}/adcreatives", creative_data)
                    creative_id = creative_result["id"]
                    results["creative_ids"].append(creative_id)

                    ad_result = self._post(f"{self.account_id}/ads", {
                        "name": variant.get("name", "Ad"),
                        "adset_id": adset_id,
                        "creative": json.dumps({"creative_id": creative_id}),
                        "status": "PAUSED",
                    })
                    results["ad_ids"].append(ad_result["id"])
                    logger.info(f"Ad created: {ad_result['id']}")

                except Exception as e:
                    results["errors"].append(f"Ad variant '{variant.get('name')}': {str(e)}")
                    logger.error(f"Error creating ad variant: {e}")

            results["success"] = True

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
        f"\"Jasmine body mist, 200k/day, women 20-35, Jakarta & Bandung, goal: traffic\"\n\n"
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
        "• Daily budget (e.g. 200k, 500rb)\n"
        "• Target audience (age, gender)\n"
        "• Locations (cities)\n"
        "• Goal (traffic, sales, awareness, leads)\n"
        "• Promo/offer details\n"
        "• Landing page URL (if not lovemaya.co)\n\n"
        "Example briefs:\n"
        "\"Rose body lotion, 300k/day, women 18-30, all Indonesia, sales\"\n\n"
        "\"New perfume launch, 500k/day, premium audience Jakarta Surabaya Bali, awareness, include promo free pouch for first 100 orders\""
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


async def handle_brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main handler — receives brief, generates campaign, asks for approval."""
    user_id = update.effective_user.id

    if not is_authorized(user_id):
        await update.message.reply_text("Sorry, you're not authorized to use this bot.")
        return

    brief_text = update.message.text
    logger.info(f"Brief received from {user_id}: {brief_text[:100]}...")

    # Step 1: Acknowledge
    status_msg = await update.message.reply_text("🧠 Generating campaign with Claude AI...")

    try:
        # Step 2: Generate campaign via Claude
        campaign = generate_campaign_with_claude(brief_text)
        logger.info(f"Campaign generated: {campaign.get('campaign_name')}")

        # Store for approval
        pending_campaigns[user_id] = campaign

        # Step 3: Send preview for approval
        summary = campaign.get("summary", "Campaign generated successfully.")
        variants_preview = ""
        for v in campaign.get("ad_variants", []):
            variants_preview += f"\n• [{v.get('angle', '')}] {v.get('primary_text', '')}"

        preview_text = (
            f"✅ Campaign Ready!\n\n"
            f"📋 {campaign.get('campaign_name', 'Campaign')}\n"
            f"🎯 {campaign.get('objective', 'TRAFFIC')}\n"
            f"💰 IDR {campaign.get('adset', {}).get('daily_budget', '200,000')}/day\n"
            f"👥 {campaign.get('adset', {}).get('gender', 'All')}, "
            f"age {campaign.get('adset', {}).get('age_min', 18)}-{campaign.get('adset', {}).get('age_max', 65)}\n"
            f"📍 {', '.join(campaign.get('adset', {}).get('locations', []))}\n\n"
            f"📝 Ad Variants:{variants_preview}\n\n"
            f"🖼 Image Prompt:\n{campaign.get('image_prompt', 'N/A')[:200]}...\n\n"
            f"🛡 Policy: {campaign.get('policy_check', 'No issues')}\n\n"
            f"What should I do?"
        )

        # Action buttons
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

        await status_msg.edit_text(preview_text, reply_markup=InlineKeyboardMarkup(keyboard))

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

    if not campaign:
        await query.edit_message_text("⚠️ No pending campaign found. Send a new brief.")
        return

    # ── EXECUTE VIA META API ──
    if action == "exec_api":
        if not META_ACCESS_TOKEN:
            await query.edit_message_text(
                "⚠️ Meta API not configured.\n"
                "Add META_ACCESS_TOKEN to your .env file.\n"
                "See SETUP_GUIDE.md for instructions."
            )
            return

        await query.edit_message_text("⏳ Creating campaign via Meta API...")

        executor = MetaAdsExecutor()
        result = executor.create_full_campaign(campaign)

        if result["success"]:
            msg = (
                f"✅ Campaign created!\n\n"
                f"Campaign ID: {result.get('campaign_id', 'N/A')}\n"
                f"Ad Set ID: {result.get('adset_id', 'N/A')}\n"
                f"Ads created: {len(result.get('ad_ids', []))}\n\n"
                f"⚠️ Status: PAUSED\n"
                f"→ Upload ad images in Ads Manager\n"
                f"→ Then activate the campaign\n"
                f"→ Or use /status {result.get('campaign_id', '')} to check"
            )
            if result.get("errors"):
                msg += f"\n\n⚠️ Warnings:\n" + "\n".join(result["errors"])
        else:
            msg = f"❌ Campaign creation failed:\n" + "\n".join(result.get("errors", ["Unknown error"]))

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
    app.add_handler(CallbackQueryHandler(handle_approval))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_brief))

    print("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
