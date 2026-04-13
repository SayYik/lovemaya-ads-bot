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
META_PAGE_ID = os.getenv("META_PAGE_ID", "")
META_IG_ACTOR_ID = os.getenv("META_IG_ACTOR_ID", "")
MANUS_API_KEY = os.getenv("MANUS_API_KEY", "")
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

RESPOND WITH VALID JSON ONLY (no markdown, no ```). Use this exact structure:

{
  "campaign_name": "Lovemaya_[Product]_[Objective]_[MonthYear]",
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
    "interests": ["Beauty", "Fragrance"]
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
  "image_prompt": "A detailed Ideogram.ai prompt for the ad image...",
  "policy_check": "No policy issues found.",
  "manus_instructions": "Step-by-step instructions for Manus AI to create this in Meta Ads Manager...",
  "summary": "A short 2-3 line summary for the Telegram reply"
}

RULES:
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

    def __init__(self, ad_account_id=None):
        self.token = META_ACCESS_TOKEN
        self.account_id = ad_account_id or AD_ACCOUNTS[DEFAULT_AD_ACCOUNT]["id"]
        self.page_id = META_PAGE_ID
        self.ig_actor_id = META_IG_ACTOR_ID
        self.base_url = "https://graph.facebook.com/v21.0"

    def _post(self, endpoint, data):
        data["access_token"] = self.token
        # Convert Python booleans to lowercase strings for form encoding
        for key, value in data.items():
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

            logger.info(f"Targeting built: {json.dumps(targeting)[:200]}")

            # ── 3. CREATE AD SET ──
            logger.info(f"Step 3: Creating ad set... (Budget type: {budget_type})")

            adset_data = {
                "name": adset.get("name", f"AdSet_{datetime.now().strftime('%Y%m%d')}"),
                "campaign_id": campaign_id,
                "billing_event": "IMPRESSIONS",
                "optimization_goal": adset.get("optimization_goal", "LINK_CLICKS").upper(),
                "targeting": json.dumps(targeting),
                "status": "PAUSED",
            }

            # ALWAYS set this field — Meta requires it on every ad set
            adset_data["is_adset_budget_sharing_enabled"] = "false"

            if budget_type == "CBO":
                # CBO: no budget on adset (campaign controls budget)
                logger.info("CBO mode: no budget on ad set (campaign controls budget)")
            else:
                # ABO: budget on adset level
                adset_data["daily_budget"] = daily_budget
                logger.info(f"ABO mode: daily_budget {daily_budget} set on ad set")

            # Add destination_type for traffic campaigns
            objective = campaign.get("objective", "OUTCOME_TRAFFIC").upper()
            if objective in ("OUTCOME_TRAFFIC",):
                adset_data["destination_type"] = "WEBSITE"

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

                    object_story_spec = {
                        "page_id": self.page_id,
                        "link_data": link_data,
                    }

                    # Add Instagram actor if available
                    if self.ig_actor_id:
                        object_story_spec["instagram_actor_id"] = self.ig_actor_id

                    creative_data = {
                        "name": variant_name,
                        "object_story_spec": json.dumps(object_story_spec),
                    }

                    logger.info(f"Creating creative: {variant_name}")
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
        "• Ad account (cpas sg, cpas my, shopify)\n"
        "• Promo/offer details\n\n"
        "🌐 LANGUAGES:\n"
        "Just mention the languages you want!\n"
        "• No language mentioned → defaults to EN, BM, CN\n"
        "• \"in english and chinese\" → 2 variants\n"
        "• \"EN BM CN Tamil\" → 4 variants\n\n"
        "Example briefs:\n"
        "\"Bath gel, MYR10/day, women 18-45, Malaysia, traffic, shopify, ABO, in EN and BM\"\n\n"
        "\"Perfume launch, MYR30/day, KL Penang, awareness, cpas my, CBO, in EN BM CN Tamil\"\n\n"
        "\"Body scrub, SGD5/day, Singapore, sales, cpas sg, chinese only\""
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

    # Step 1: Detect ad account and budget type from brief
    detected_account = detect_ad_account(brief_text)
    budget_type = detect_budget_type(brief_text)

    # Step 2: Acknowledge
    budget_label = "Campaign Budget (CBO)" if budget_type == "CBO" else "Ad Set Budget (ABO)"
    status_msg = await update.message.reply_text(
        f"🧠 Generating campaign with Claude AI...\n"
        f"📂 Ad Account: {detected_account['name']}\n"
        f"💰 Budget Type: {budget_label}"
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
        logger.info(f"Using ad account: {campaign['_ad_account']['name']} ({campaign['_ad_account']['id']})")
        logger.info(f"Budget type: {budget_type}")

        # Store for approval
        pending_campaigns[user_id] = campaign

        # Step 4: Send preview for approval
        summary = campaign.get("summary", "Campaign generated successfully.")
        variants_preview = ""
        for v in campaign.get("ad_variants", []):
            lang = v.get("language", "")
            lang_tag = f" ({lang})" if lang else ""
            variants_preview += f"\n• [{v.get('angle', '')}{lang_tag}] {v.get('primary_text', '')}"

        acct = campaign.get("_ad_account", detected_account)
        currency = acct.get("currency", "MYR")
        budget = campaign.get("adset", {}).get("daily_budget", "?")
        budget_label = "Campaign Budget (CBO)" if budget_type == "CBO" else "Ad Set Budget (ABO)"

        preview_text = (
            f"✅ Campaign Ready!\n\n"
            f"📂 Ad Account: {acct['name']}\n"
            f"📋 {campaign.get('campaign_name', 'Campaign')}\n"
            f"🎯 {campaign.get('objective', 'TRAFFIC')}\n"
            f"💰 {currency} {budget} (cents)/day — {budget_label}\n"
            f"👥 {campaign.get('adset', {}).get('gender', 'All')}, "
            f"age {campaign.get('adset', {}).get('age_min', 18)}-{campaign.get('adset', {}).get('age_max', 65)}\n"
            f"📍 {', '.join(str(l) for l in campaign.get('adset', {}).get('locations', []))}\n\n"
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

        acct = campaign.get("_ad_account", AD_ACCOUNTS[DEFAULT_AD_ACCOUNT])
        await query.edit_message_text(f"⏳ Creating campaign via Meta API...\n📂 Account: {acct['name']}")

        executor = MetaAdsExecutor(ad_account_id=acct["id"])
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
            if result.get("warnings"):
                msg += f"\n\n⚠️ Notes:\n" + "\n".join(result["warnings"])
            if result.get("errors"):
                msg += f"\n\n⚠️ Some ads had issues:\n" + "\n".join(result["errors"])
        else:
            errors_text = "\n".join(result.get("errors", ["Unknown error"]))
            msg = (
                f"❌ Campaign creation failed:\n\n"
                f"{errors_text}\n\n"
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
    app.add_handler(CallbackQueryHandler(handle_approval))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_brief))

    print("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
