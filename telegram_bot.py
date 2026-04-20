#!/usr/bin/env python3
BOT_VERSION = "v6.0"  # Change this to verify Railway deploys the latest file
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
META_PIXEL_ID = os.getenv("META_PIXEL_ID", "769767095050716").strip()  # Facebook Pixel — hardcoded fallback
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

# Store pending briefs waiting for product selection
pending_product_selection = {}

# ─────────────────────────────────────────────
# PRODUCT CATALOG — auto-matches products from brief
# ─────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(__file__)
PRODUCTS_DIR = os.path.join(SCRIPT_DIR, "products")
CATALOG_PATH = os.path.join(PRODUCTS_DIR, "catalog.json")


def find_image(img_name: str) -> str | None:
    """Find an image file — checks products/ folder first, then root directory."""
    # Check products/ subfolder first
    path1 = os.path.join(PRODUCTS_DIR, img_name)
    if os.path.exists(path1):
        return path1
    # Check root directory (where GitHub web upload puts files)
    path2 = os.path.join(SCRIPT_DIR, img_name)
    if os.path.exists(path2):
        return path2
    return None


FALLBACK_CATALOG = [
    {"id":"bath_gel","name":"Bath Gel","taxonomy_code":"BATH GEL","keywords":["bath gel","shower gel","body wash"],
     "images":["LM_BG_Ocean_Shadow.png","LM_BG_WildRose_Background.png","Copy of LOVEMAYA-20252614-withshadow.png","Copy of LOVEMAYA-20252617(withshadow).png","Copy of LOVEMAYA-20252628-withshadow.png","Copy of Copy of 250513 - Love Maya D3 7.jpg","SL Love Maya-5 (new shadow).png","SL Love Maya-6 (new shadow).png","SL Love Maya-8 (new shadow).png"],
     "variants":{"ocean":["LM_BG_Ocean_Shadow.png"],"wild rose":["LM_BG_WildRose_Background.png"],"sunrise":["Copy of LOVEMAYA-20252614-withshadow.png"],"amberlight":["Copy of LOVEMAYA-20252617(withshadow).png"],"morning zest":["Copy of LOVEMAYA-20252628-withshadow.png"],"petals":["Copy of Copy of 250513 - Love Maya D3 7.jpg"],"cedar rain":["SL Love Maya-5 (new shadow).png"],"wood sage":["SL Love Maya-6 (new shadow).png"],"earth":["SL Love Maya-8 (new shadow).png"]},
     "description":"Love Maya Bath Gel — Niacinamide & Hyaluronic Acid, 400ML"},
    {"id":"body_lotion","name":"Body Lotion","taxonomy_code":"BODY LOTION","keywords":["body lotion","lotion","moisturizer","moisturiser"],
     "images":["LM_BL_Ocean_WithBackground.png","Copy of LOVEMAYA-20252625-withoutshadow.png","SL Love Maya-7 (new shadow).png"],
     "variants":{"ocean":["LM_BL_Ocean_WithBackground.png"],"geranium":["Copy of LOVEMAYA-20252625-withoutshadow.png"],"earth":["SL Love Maya-7 (new shadow).png"]},
     "description":"Love Maya Body Lotion — Aloe Vera & Ceramide, 400ML"},
    {"id":"hand_cream","name":"Hand Cream","taxonomy_code":"HAND CREAM","keywords":["hand cream","hand lotion","hand care"],
     "images":["Copy of Copy of LOVE MAYA12630.jpg","Copy of LOVE MAYA12629.jpg","Copy of LOVE MAYA12635.jpg"],
     "variants":{"earth":["Copy of Copy of LOVE MAYA12630.jpg"],"geranium":["Copy of LOVE MAYA12629.jpg"],"ocean":["Copy of LOVE MAYA12635.jpg"]},
     "description":"Love Maya Hand Cream — 30ML travel size"},
    {"id":"perfume","name":"Perfume","taxonomy_code":"PERFUME","keywords":["perfume","eau de","fragrance","scent","parfum"],
     "images":["LM PERFUME 20265869 (1).png","LM PERFUME 20265871 (1).png","LM PERFUME 20265888 (1).png"],
     "variants":{"wood sage":["LM PERFUME 20265869 (1).png"],"ocean":["LM PERFUME 20265871 (1).png"],"earth":["LM PERFUME 20265888 (1).png"]},
     "description":"Love Maya Eau de Parfum — signature scent collection"},
    {"id":"bundle","name":"Bundle / Gift Set","taxonomy_code":"BUNDLE","keywords":["bundle","gift set","combo","set","package","group buy","travel set","pouch"],
     "images":["1O1A6790 E.jpg"],"variants":{},"description":"Love Maya Bundle / Gift Set"},
    {"id":"body_mist","name":"Body Mist","taxonomy_code":"BODY MIST","keywords":["body mist","mist","fragrance mist","body spray"],
     "images":[],"variants":{},"description":"Love Maya Body Mist"},
    {"id":"body_scrub","name":"Body Scrub","taxonomy_code":"BODY SCRUB","keywords":["body scrub","scrub","exfoliant","exfoliator"],
     "images":[],"variants":{},"description":"Love Maya Body Scrub"},
    {"id":"hair_shampoo","name":"Hair Shampoo","taxonomy_code":"HAIR SHAMPOO","keywords":["shampoo","hair shampoo","hair wash","hair care"],
     "images":[],"variants":{},"description":"Love Maya Hair Shampoo"},
    {"id":"mixed_series","name":"Mixed / All Products","taxonomy_code":"MIXED SERIES","keywords":["mixed","mixed series","all products","multi product"],
     "images":["1O1A6790 E.jpg"],"variants":{},"description":"Love Maya multiple products"},
]


def load_product_catalog() -> list:
    """Load product catalog from JSON file, with hardcoded fallback."""
    try:
        with open(CATALOG_PATH, "r") as f:
            data = json.load(f)
        products = data.get("products", [])
        if products:
            return products
    except Exception as e:
        logger.warning(f"Could not load product catalog: {e}")
    # Fallback to hardcoded catalog
    logger.info("Using fallback product catalog")
    return FALLBACK_CATALOG


def detect_product(text: str) -> list:
    """
    Detect which product(s) the user is referring to in their brief.
    Returns list of matched products sorted by keyword match length (best match first).
    """
    catalog = load_product_catalog()
    if not catalog:
        return []

    text_lower = text.lower()
    matches = []

    for product in catalog:
        for keyword in sorted(product.get("keywords", []), key=len, reverse=True):
            if keyword in text_lower:
                matches.append(product)
                break  # One match per product is enough

    return matches


def get_product_images(product: dict, brief_text: str = "") -> list:
    """
    Get available image file paths for a product (only files that actually exist).
    If brief mentions a specific variant (e.g. 'ocean', 'wild rose'), show only that variant's images.
    Otherwise show all images for the product.
    """
    # Check if brief mentions a specific variant
    variants = product.get("variants", {})
    if brief_text and variants:
        brief_lower = brief_text.lower()
        for variant_name, variant_images in variants.items():
            if variant_name in brief_lower:
                available = []
                for img_name in variant_images:
                    img_path = find_image(img_name)
                    if img_path:
                        available.append(img_path)
                if available:
                    logger.info(f"Variant matched: {variant_name} ({len(available)} images)")
                    return available

    # No variant match — return all product images
    available = []
    for img_name in product.get("images", []):
        img_path = find_image(img_name)
        if img_path:
            available.append(img_path)
    return available


def get_all_products_for_picker() -> list:
    """Get all products from catalog for the inline picker buttons."""
    return load_product_catalog()


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

=====================
TAXONOMY NAMING SYSTEM
=====================
You MUST use these taxonomy codes to generate campaign_name, adset.name, and ad variant names.

CAMPAIGN NAME FORMAT: Funnel | Brand | Destination | BizObjective | Year | CampaignObjective | BudgetType | BidStrategy
Example: PP | LM | SHOPIFY | BAU | 2026 | SAL | CBO | HV

ADSET NAME FORMAT: Audience | AudienceSetup | ProductCategory | PerformanceGoal | Language | Placement | FreeSection
Example: CHI Female 18-65+ | A+ | BATH GEL | PUR | MIXED | A+ | Raya Sale

AD NAME FORMAT: AdFormat | Angle | Month | CreativeName
Example: VID | DISCOUNT | Apr | KOL Coco Bath Gel Promo

--- TAXONOMY CODES ---

FUNNEL:
  PP = Prospecting (cold audience, new customers)
  RT = Retargeting (warm audience, past visitors/engagers)

BRAND:
  LM = Love Maya
  BG = Bath Garden
  LMSGP = Love Maya Singapore
  BGSGP = Bath Garden Singapore

DESTINATION PLATFORM:
  SHOPIFY = Shopify website campaigns
  SHOPEE = Shopee CPAS campaigns
  RETAIL = Retail / offline store campaigns
  OTH = Other platforms

BUSINESS OBJECTIVE:
  BAU = Business as usual (always-on, evergreen)
  CAMPAIGN = Time-limited campaign (sale, launch, event)

CAMPAIGN OBJECTIVE (maps to Meta objective):
  AWA = Awareness → OUTCOME_AWARENESS
  TRF = Traffic → OUTCOME_TRAFFIC
  EGM = Engagement → OUTCOME_ENGAGEMENT
  LEADS = Leads → OUTCOME_LEADS
  SAL = Sales → OUTCOME_SALES

BUDGET TYPE (always use CBO):
  CBO = Campaign Budget Optimization

BID STRATEGY:
  HV = Highest Volume (default, = LOWEST_COST_WITHOUT_CAP)
  CPA = Cost per result goal
  ROAS = ROAS goal
  BC = Bid cap

AUDIENCE (describe the target):
  Format: "[Descriptor] [Age range]" e.g. "CHI Female 18-65+", "Broad 18-64", "KV BM Malay 18-65+"
  For retargeting: use codes like "PUR P60D" (Purchasers Past 60 Days), "Cart Abandoners P90D", "WV P90D" (Website Visitors), "PE P90D" (Page Engagers), "VC P90D" (View Content), "LAL 3% 1PD" (Lookalike 3%), "1PD" (1st Party Data)

AUDIENCE SETUP:
  ORI = Original/Manual targeting
  A+ = Advantage+ audience

PRODUCT CATEGORY:
  BATH GEL, BODY LOTION, BODY MIST, BODY SCRUB, BUNDLE, FREEDOM SERIES, HOPE SERIES, GROUNDED SERIES, MIXED SERIES, HAIR SHAMPOO, PERFUME

PERFORMANCE GOAL:
  CVS NO = Number of Conversions
  VAL CVS = Value of Conversions
  ATC = Add to Cart (CPAS only)
  PUR = Purchase (CPAS only)
  VC = View Content (CPAS only)
  REACH = Reach
  IMP = Impressions
  LC = Link Clicks
  LPV = Landing Page Views
  VV = Video Views
  PE = Post Engagements
  DM = Direct Messages
  ER = Event Responses

LANGUAGE:
  ALL = All languages
  BM = Bahasa Malaysia
  CHI = Chinese
  ENG = English
  MIXED = Multiple languages

PLACEMENT:
  A+ = Advantage+ (automatic, recommended)
  FB = Facebook only
  IG = Instagram only
  Manual = Manual placement
  Threads = Threads

AD FORMAT:
  STA = Static image
  VID = Video
  CLN = Collection
  CAR = Carousel

ANGLE (ad creative approach):
  REVIEW = User reviews/unboxing/feedback
  COMPARISON = Highlight differences vs competitors
  REASON = "Why buy/choose us" (e.g. support local)
  FEATURE = Key functions, USP
  SOLUTION = Problem-solving (body odour, dry skin, etc.)
  CUR = Drive curiosity, tease to spark interest
  TREND = Current trends/viral/meme
  STG = Storytelling (KOL daily routine, etc.)
  DISCOUNT = Limited-time deals, price drops
  FOMO = Urgency, limited edition/stock

MONTH: Jan, Feb, Mar, Apr, May, Jun, Jul, Aug, Sep, Oct, Nov, Dec
(Use the month the ad is uploaded/created, from today's date)

--- END TAXONOMY CODES ---

OBJECTIVE SELECTION GUIDE:
Choose the best objective based on the brief's goal:
- "traffic" / "website visits" / "clicks" → OUTCOME_TRAFFIC (taxonomy: TRF)
- "sales" / "conversions" / "purchase" → OUTCOME_SALES (taxonomy: SAL)
- "awareness" / "reach" / "branding" → OUTCOME_AWARENESS (taxonomy: AWA)
- "leads" / "sign up" / "form" → OUTCOME_LEADS (taxonomy: LEADS)
- "engagement" / "likes" / "comments" → OUTCOME_ENGAGEMENT (taxonomy: EGM)
If the user doesn't specify, choose the best objective for the product and context.

LANGUAGE STRATEGY — DYNAMIC, USER-CONTROLLED:
The user specifies which languages they want. Generate ONE ad variant per language.

How to detect languages from the brief:
- "english" / "EN" → English (taxonomy: ENG)
- "malay" / "bahasa" / "BM" → Bahasa Malaysia (taxonomy: BM)
- "chinese" / "CN" / "mandarin" → Chinese (taxonomy: CHI)
- "tamil" / "TM" → Tamil
- "all" → all languages (taxonomy: ALL)
- "mixed" → mixed (taxonomy: MIXED)
If no languages specified, default to 3 variants: English, BM, Chinese. Use taxonomy MIXED for the adset language field.

TONE GUIDE per language:
- English → clean, modern, aspirational
- Bahasa Malaysia → warm, relatable, casual (not formal)
- Chinese (简体中文) → elegant, concise, beauty-focused
- Tamil → respectful, family-oriented, warm

Each variant should convey the SAME core message/offer but LOCALIZED naturally (not a direct translation).

IMPORTANT DATE: Today's date will be provided at the end of the brief. ALWAYS use that date for Year and Month in taxonomy names.

INTEREST TARGETING — USER-CONTROLLED:
- If the user specifies interests → use ONLY those exact interests
- If not → choose 3-5 relevant interests for the product
- Always include interests in the JSON

RESPOND WITH VALID JSON ONLY (no markdown, no ```). Use this exact structure:

{
  "taxonomy": {
    "funnel": "PP",
    "brand": "LM",
    "destination": "SHOPIFY",
    "biz_objective": "BAU",
    "year": "2026",
    "campaign_objective": "SAL",
    "budget_type": "CBO",
    "bid_strategy": "HV",
    "audience": "Female 18-45",
    "audience_setup": "A+",
    "product_category": "BATH GEL",
    "performance_goal": "LC",
    "language": "MIXED",
    "placement": "A+",
    "free_section": ""
  },
  "campaign_name": "PP | LM | SHOPIFY | BAU | 2026 | SAL | CBO | HV",
  "ad_account": "shopify_my",
  "objective": "OUTCOME_SALES",
  "currency": "MYR",
  "website_url": "https://lovemaya.co",
  "adset": {
    "name": "Female 18-45 | A+ | BATH GEL | LC | MIXED | A+",
    "daily_budget": 1000,
    "age_min": 18,
    "age_max": 45,
    "gender": "women",
    "optimization_goal": "LINK_CLICKS",
    "locations": ["Malaysia"],
    "interests": ["Beauty", "Fragrance"],
    "languages": ["en", "ms", "zh_CN"]
  },
  "ad_variants": [
    {
      "name": "STA | DISCOUNT | Apr | EN Bath Gel Promo",
      "language": "English",
      "ad_format": "STA",
      "angle": "DISCOUNT",
      "primary_text": "under 125 chars — in that language",
      "headline": "under 40 chars — in that language",
      "description": "under 30 chars — in that language",
      "cta": "SHOP_NOW"
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
- ALWAYS use the taxonomy naming format with pipe " | " separators
- campaign_name = Funnel | Brand | Destination | BizObjective | Year | CampaignObjective | BudgetType | BidStrategy
- adset.name = Audience | AudienceSetup | ProductCategory | PerformanceGoal | Language | Placement | FreeSection (omit FreeSection if empty)
- Each ad variant name = AdFormat | Angle | Month | CreativeName
- ONLY create ONE campaign, ONE ad set, and one ad per language. Never duplicate.
- Generate ONE variant per language the user requests
- If no languages specified, default to 3: English, Bahasa Malaysia, Chinese (Simplified)
- Each variant is LOCALIZED (not a direct translation) — adapt the feel for that audience
- Use different angles for each variant (REVIEW, DISCOUNT, FEATURE, SOLUTION, FOMO, etc.)
- Primary text under 125 chars, headline under 40, description under 30
- CTA must be: SHOP_NOW, LEARN_MORE, SIGN_UP, BOOK_NOW, or GET_OFFER
- ALWAYS include "language", "ad_format", and "angle" fields in each variant
- Budget is in MYR (Malaysian Ringgit). MYR 10/day = daily_budget: 1000 (Meta uses cents)
- Default targeting: Malaysia. Use specific states/cities if mentioned
- For retargeting briefs (mention "retarget", "warm audience", "past buyers"): use funnel RT
- For BAU (always-on, no specific promo): use biz_objective BAU
- For time-limited (sale, launch, event, promo): use biz_objective CAMPAIGN
- Budget type is always CBO, bid strategy default is HV unless user specifies otherwise
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

HIGGSFIELD_API_KEY = os.getenv("HIGGSFIELD_API_KEY", "bd111f69-3026-4946-ab32-6806bbb99323")  # hardcoded fallback
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
        # Remove any bid_amount/bid_cap from adsets (CBO handles bidding at campaign level)
        if "adsets" in endpoint:
            data.pop("bid_amount", None)
            data.pop("bid_cap", None)
            data.pop("bid_strategy", None)
        # Convert Python booleans to string for form encoding
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
        results = {"success": False, "errors": [], "warnings": [], "failed_step": "init"}

        try:
            # ── VALIDATION ──
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
            results["failed_step"] = "1-campaign"
            # ALWAYS use CBO — Meta requires is_adset_budget_sharing_enabled for ABO
            # which causes errors. CBO avoids this entirely and performs better.
            budget_type = "CBO"
            adset = campaign.get("adset", {})
            daily_budget = adset.get("daily_budget", 200000)
            daily_budget = str(int(float(str(daily_budget).replace(",", "").replace(".", ""))))

            # Resolve objective (may be downgraded if pixel not set)
            objective = campaign.get("objective", "OUTCOME_TRAFFIC").upper()
            if objective == "OUTCOME_SALES" and not META_PIXEL_ID:
                logger.warning("OUTCOME_SALES but no pixel — downgrading to OUTCOME_TRAFFIC")
                objective = "OUTCOME_TRAFFIC"
                results["warnings"].append("⚠️ No Facebook Pixel → changed Sales to Traffic. Add META_PIXEL_ID in Railway for Sales.")

            results["debug_resolved_objective"] = objective
            logger.info(f"Step 1: Creating campaign... (Budget type: CBO, Objective: {objective})")
            campaign_data = {
                "name": campaign["campaign_name"],
                "objective": objective,
                "status": "PAUSED",
                "special_ad_categories": json.dumps([]),
                "daily_budget": daily_budget,
                "is_campaign_budget_optimization_on": "true",
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            }
            logger.info(f"CBO mode: daily_budget {daily_budget} on campaign, Meta distributes to ad sets")

            logger.info(f"Campaign POST data: {json.dumps({k:v for k,v in campaign_data.items()})}")
            camp_result = self._post(f"{self.account_id}/campaigns", campaign_data)
            campaign_id = camp_result["id"]
            results["campaign_id"] = campaign_id
            results["budget_type"] = budget_type
            logger.info(f"✅ Campaign created: {campaign_id}")

            # ── 2. BUILD TARGETING ──
            results["failed_step"] = "2-targeting"
            logger.info("Step 2: Building targeting...")
            adset = campaign.get("adset", {})
            targeting = {
                "age_min": int(adset.get("age_min", 20)),
                "age_max": int(adset.get("age_max", 35)),
            }

            gender_map = {"women": [2], "female": [2], "men": [1], "male": [1]}
            genders = gender_map.get(str(adset.get("gender", "")).lower(), [])
            if genders:
                targeting["genders"] = genders

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
                logger.warning("No cities found, falling back to Malaysia country targeting")
                targeting["geo_locations"] = {"countries": ["MY"]}
                results["warnings"].append("Cities not found, used Malaysia-wide targeting instead")

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
            locale_ids = []
            adset_languages = adset.get("languages", [])
            if adset_languages:
                for lang_code in adset_languages:
                    lid = LOCALE_MAP.get(lang_code.lower().replace("-", "_"))
                    if lid and lid not in locale_ids:
                        locale_ids.append(lid)
            else:
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

            audience_type = campaign.get("_audience_type", "MANUAL")
            if audience_type == "ADV+":
                targeting["targeting_automation"] = {"advantage_audience": 1}
            else:
                targeting["targeting_automation"] = {"advantage_audience": 0}

            logger.info(f"✅ Targeting built: {json.dumps(targeting)[:200]}")

            # ── 3. CREATE AD SET ──
            results["failed_step"] = "3-adset"
            logger.info(f"Step 3: Creating ad set... (Budget type: {budget_type})")

            OBJECTIVE_CONFIG = {
                "OUTCOME_TRAFFIC": {
                    "optimization_goal": "LINK_CLICKS",
                    "destination_type": "WEBSITE",
                },
                "OUTCOME_SALES": {
                    "optimization_goal": "OFFSITE_CONVERSIONS",
                    "destination_type": "WEBSITE",
                    "promoted_object": {"pixel_id": META_PIXEL_ID, "custom_event_type": "PURCHASE"},
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
            results["debug_optimization_goal"] = opt_goal
            results["debug_destination_type"] = config.get("destination_type", "none")
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

            # CBO: budget is on the campaign, not the ad set
            logger.info("CBO mode: no budget on ad set (campaign controls budget)")

            logger.info(f"Adset POST data keys: {list(adset_data.keys())}")
            adset_result = self._post(f"{self.account_id}/adsets", adset_data)
            adset_id = adset_result["id"]
            results["adset_id"] = adset_id
            logger.info(f"✅ Ad Set created: {adset_id}")

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

async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Debug env vars and product catalog."""
    import os as _os
    meta_vars = {k: v[:8] + "..." if len(v) > 8 else v for k, v in _os.environ.items() if "META" in k or "PIXEL" in k}
    raw_pixel = _os.getenv("META_PIXEL_ID", "<NOT FOUND>")
    raw_higgs = _os.getenv("HIGGSFIELD_API_KEY", "<NOT FOUND>")

    # Check product catalog and images
    catalog_exists = _os.path.exists(CATALOG_PATH)
    products_dir_exists = _os.path.isdir(PRODUCTS_DIR)
    products_files = []
    if products_dir_exists:
        products_files = [f for f in _os.listdir(PRODUCTS_DIR) if f != "catalog.json"]

    # Check image detection
    catalog = load_product_catalog()
    catalog_count = len(catalog)
    first_product = catalog[0] if catalog else {}
    first_keywords = first_product.get("keywords", [])
    first_name = first_product.get("name", "NONE")
    test_product = None
    test_images = []
    for p in catalog:
        if "bath gel" in [k.lower() for k in p.get("keywords", [])]:
            test_product = p
            test_images = get_product_images(p, "ocean bath gel")
            break

    # Also try raw catalog read
    try:
        with open(CATALOG_PATH, "r") as _f:
            raw_first = _f.read(200)
    except:
        raw_first = "FAILED TO READ"

    await update.message.reply_text(
        f"🔧 DEBUG INFO:\n\n"
        f"Version: {BOT_VERSION}\n"
        f"Pixel: '{META_PIXEL_ID}'\n"
        f"Higgsfield: {'✅ SET' if raw_higgs != '<NOT FOUND>' else '❌ NOT FOUND'}\n"
        f"Higgsfield var: {'✅ SET' if HIGGSFIELD_API_KEY else '❌ EMPTY'}\n\n"
        f"📂 PRODUCTS:\n"
        f"PRODUCTS_DIR: {PRODUCTS_DIR}\n"
        f"Dir exists: {products_dir_exists}\n"
        f"Catalog exists: {catalog_exists}\n"
        f"Files in products/: {len(products_files)}\n"
        f"Files: {', '.join(products_files[:5])}{'...' if len(products_files) > 5 else ''}\n\n"
        f"🔍 CATALOG:\n"
        f"Products loaded: {catalog_count}\n"
        f"1st product: {first_name}\n"
        f"1st keywords: {first_keywords}\n"
        f"Raw catalog: {raw_first}\n\n"
        f"🔍 TEST DETECTION:\n"
        f"Bath gel found: {test_product is not None}\n"
        f"Ocean images: {len(test_images)}\n"
        f"Image paths: {test_images[:2] if test_images else 'NONE'}\n\n"
        f"All META vars:\n" + "\n".join(f"  {k}={v}" for k, v in meta_vars.items())
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message."""
    user = update.effective_user
    debug_pixel = "✅ SET" if META_PIXEL_ID else "❌ NOT SET"
    await update.message.reply_text(
        f"Hey {user.first_name}! 👋\n\n"
        f"I'm the Lovemaya Ads Engine {BOT_VERSION}\n"
        f"Send me a brief and I'll create a full Meta campaign.\n\n"
        f"Example:\n"
        f"\"Bath gel, MYR10/day, women 18-45, Malaysia, traffic, shopify, in EN BM CN\"\n\n"
        f"📂 Ad Accounts:\n"
        f"• \"cpas sg\" → CPAS Singapore\n"
        f"• \"cpas my\" → CPAS Malaysia\n"
        f"• \"shopify\" → Shopify Malaysia (default)\n\n"
        f"💰 Budget Types:\n"
        f"• \"CBO\" → Campaign budget (shared)\n"
        f"• \"ABO\" → Ad set budget (default)\n\n"
        f"📊 Performance:\n"
        f"/performance — SCALE/IMPROVE/KILL analysis\n"
        f"/spy [brand] — Competitor ad research\n"
        f"/drill [keyword] — Campaign deep-dive\n\n"
        f"Commands:\n"
        f"/start — This message\n"
        f"/status [campaign_id] — Check campaign status\n"
        f"/help — Tips for writing briefs\n\n"
        f"🔧 Pixel: {debug_pixel} | Version: {BOT_VERSION}"
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
        "📊 PERFORMANCE COMMANDS:\n"
        "/performance — Analyze all campaigns (SCALE/IMPROVE/KILL)\n"
        "/performance 3d — Last 3 days | 7d | 14d | 30d | today\n"
        "/drill [keyword] — Drill into ad-level metrics for a campaign\n"
        "/spy [brand] — Search competitor ads on Meta Ad Library\n\n"
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
    """Research a competitor brand's ad strategy and save insights."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Sorry, you're not authorized.")
        return

    brand_text = " ".join(context.args) if context.args else ""
    if not brand_text:
        await update.message.reply_text(
            "🔍 Tell me a brand to study! I'll research their ads and save the insights.\n\n"
            "Examples:\n"
            "/learn Sol de Janeiro\n"
            "/learn Glossier\n"
            "/learn CeraVe\n"
            "/learn The Ordinary\n\n"
            "📸 You can also send me a SCREENSHOT of any ad with the caption 'analyze this' — I'll break down the hooks, angles, and copy style."
        )
        return

    await update.message.reply_text(f"🔍 Studying {brand_text}'s ad strategy...")

    try:
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

        # Get existing brand learnings for context
        memories = load_memory()
        brand_insights = [m for m in memories if m.get("type") == "brand_learning"]
        existing_brands = ", ".join([m.get("brand", "") for m in brand_insights]) if brand_insights else "none yet"

        learn_prompt = f"""You are a DTC advertising analyst specializing in personal care and beauty brands.

Research and analyze the Meta/Instagram advertising strategy of: {brand_text}

Provide your analysis in TWO parts:

PART 1 — BRAND ANALYSIS (for the user to read):
1. Brand positioning and unique selling points
2. Top 3 ad copy angles they use most (with real examples if possible)
3. Visual style — lighting, colors, models, product placement
4. Creative formats — static, video, carousel, UGC, KOL
5. Target audience and messaging approach
6. What makes their ads STOP the scroll

PART 2 — ACTIONABLE INSIGHTS FOR LOVEMAYA (these will be saved):
Write exactly 5 bullet points that Lovemaya can directly apply. Each bullet should be a specific, actionable insight — not generic advice. Format each as:
• [ANGLE/HOOK TYPE]: [Specific thing to try] — Example: "[actual ad copy example adapted for Lovemaya]"

Example format:
• [SOCIAL PROOF]: Use "X people bought this week" counters — Example: "2,847 Malaysians switched to Love Maya this month"
• [SENSORY]: Describe the scent/feel experience — Example: "Close your eyes. Imagine jasmine on warm skin."

{DTC_KNOWLEDGE}

Brands already studied: {existing_brands}
Be specific, practical, and give copy examples Lovemaya can test immediately."""

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=3000,
            messages=[{"role": "user", "content": learn_prompt}]
        )

        analysis = message.content[0].text.strip()

        # Save key insights to memory
        insight_entry = {
            "type": "brand_learning",
            "brand": brand_text,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "insights": analysis[-1500:]  # Save the actionable insights portion
        }
        add_memory(f"[BRAND STUDY: {brand_text}] {analysis[-800:]}")
        logger.info(f"Saved brand insights for: {brand_text}")

        # Send analysis
        if len(analysis) > 4000:
            parts = [analysis[i:i+4000] for i in range(0, len(analysis), 4000)]
            for part in parts:
                await update.message.reply_text(part)
        else:
            await update.message.reply_text(f"🔍 Brand Analysis: {brand_text}\n\n{analysis}")

        await update.message.reply_text(
            f"✅ Insights from {brand_text} saved to memory!\n"
            f"I'll reference these learnings when creating your future campaigns.\n\n"
            f"🧠 Use /memory to see all saved insights."
        )

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def handle_photo_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Analyze a competitor ad screenshot sent as a photo."""
    if not is_authorized(update.effective_user.id):
        return

    caption = (update.message.caption or "").lower()
    # Only trigger on photos with relevant captions
    trigger_words = ["analyze", "analyse", "learn", "study", "break down", "breakdown", "what can i learn", "ad analysis"]
    if not any(word in caption for word in trigger_words):
        return  # Not an analysis request, ignore

    await update.message.reply_text("🔍 Analyzing this ad...")

    try:
        # Download the photo (get highest resolution)
        photo = update.message.photo[-1]  # Highest res
        photo_file = await photo.get_file()
        photo_bytes = await photo_file.download_as_bytearray()

        import base64
        photo_b64 = base64.b64encode(bytes(photo_bytes)).decode("utf-8")

        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

        analyze_prompt = f"""You are a DTC ad creative analyst. Analyze this ad screenshot and extract actionable insights for Lovemaya (Malaysian body care brand: bath gel, body lotion, body mist, body scrub).

Break down:
1. HOOK — What stops the scroll? First 3 seconds / first line of text
2. ANGLE — What persuasion angle is used? (social proof, FOMO, benefit-led, emotional, curiosity, etc.)
3. COPY STRUCTURE — How is the text structured? (problem → solution, testimonial, list, story)
4. VISUAL STYLE — Colors, composition, product placement, models, text overlay
5. CTA — What action does it drive?
6. WHAT WORKS — Why would this ad convert?
7. LOVEMAYA ADAPTATION — Write 2 specific ad copy examples adapting this approach for Lovemaya products

Caption from user: {update.message.caption or 'No caption provided'}

Be specific and practical. Focus on what Lovemaya can steal and adapt."""

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": photo_b64}},
                    {"type": "text", "text": analyze_prompt}
                ]
            }]
        )

        analysis = message.content[0].text.strip()

        # Save to memory
        brand_hint = update.message.caption.replace("analyze", "").replace("this", "").strip() or "competitor ad"
        add_memory(f"[AD SCREENSHOT ANALYSIS: {brand_hint}] {analysis[-500:]}")

        if len(analysis) > 4000:
            parts = [analysis[i:i+4000] for i in range(0, len(analysis), 4000)]
            for part in parts:
                await update.message.reply_text(part)
        else:
            await update.message.reply_text(f"🔍 Ad Analysis:\n\n{analysis}")

        await update.message.reply_text("✅ Insights saved! I'll use these patterns in future campaigns.")

    except Exception as e:
        await update.message.reply_text(f"Error analyzing image: {e}")


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


# ─────────────────────────────────────────────
# PERFORMANCE ANALYZER — Pull live Meta data, analyze, recommend
# ─────────────────────────────────────────────

PERFORMANCE_RULES = {
    "SCALE": {
        "description": "Performing well — increase budget",
        "conditions": "ROAS >= 3.0 OR (CTR >= 2.0% AND CPC <= MYR 1.50)"
    },
    "IMPROVE": {
        "description": "Decent but needs optimization",
        "conditions": "ROAS 1.5-3.0 OR (CTR 1.0-2.0% AND moderate CPC)"
    },
    "KILL": {
        "description": "Underperforming — turn off",
        "conditions": "ROAS < 1.5 AND running > 3 days AND spend > MYR 50"
    },
}

ANALYSIS_PROMPT = """You are a Meta Ads performance analyst for Lovemaya (Malaysian body care brand).

Analyze these campaign performance metrics and give actionable recommendations.

CAMPAIGN DATA:
{campaign_data}

PERFORMANCE RULES:
- SCALE (🟢): ROAS >= 3.0, or CTR >= 2.0% with CPC under MYR 1.50. These are winners — increase budget 20-50%.
- IMPROVE (🟡): ROAS 1.5-3.0, or decent CTR but high CPC. Test new creative, adjust audience, or change bid.
- KILL (🔴): ROAS < 1.5 after spending > MYR 50 over 3+ days. Not working — turn off.
- WATCH (👀): Too early to tell — less than MYR 30 spent or running < 2 days.

For EACH campaign/ad set, provide:
1. Verdict: SCALE / IMPROVE / KILL / WATCH
2. Why: cite the specific metrics driving the verdict
3. Action: ONE specific thing to do (increase budget by X%, change audience to Y, kill and reallocate to Z, etc.)

Also include:
- Overall account health summary (1-2 sentences)
- Top performer and why
- Biggest waste and why
- One strategic recommendation for next week

BRAND LEARNINGS:
{learnings}

Be direct and specific. Use numbers. No fluff."""


async def cmd_performance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pull live Meta Ads performance and give SCALE/IMPROVE/KILL recommendations."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("Sorry, you're not authorized.")
        return

    # Parse time range from command args
    args = context.args
    date_preset = "last_7d"
    range_label = "Last 7 Days"
    if args:
        arg = args[0].lower()
        presets = {
            "today": ("today", "Today"),
            "yesterday": ("yesterday", "Yesterday"),
            "3d": ("last_3d", "Last 3 Days"),
            "7d": ("last_7d", "Last 7 Days"),
            "14d": ("last_14d", "Last 14 Days"),
            "30d": ("last_30d", "Last 30 Days"),
            "month": ("this_month", "This Month"),
        }
        if arg in presets:
            date_preset, range_label = presets[arg]

    await update.message.reply_text(f"📊 Pulling performance data ({range_label})...")

    try:
        # Pull data from ALL ad accounts
        all_campaign_data = []
        for acct_key, acct in AD_ACCOUNTS.items():
            acct_id = acct["id"]
            currency = acct.get("currency", "MYR")

            # Get campaigns with insights
            campaigns_url = f"https://graph.facebook.com/v21.0/{acct_id}/campaigns"
            params = {
                "access_token": META_ACCESS_TOKEN,
                "fields": "name,status,objective,daily_budget,lifetime_budget,start_time",
                "filtering": json.dumps([{"field": "status", "operator": "IN", "value": ["ACTIVE", "PAUSED"]}]),
                "limit": 50,
            }
            resp = requests.get(campaigns_url, params=params, timeout=30)
            campaigns = resp.json().get("data", [])

            for camp in campaigns:
                camp_id = camp["id"]
                camp_name = camp.get("name", "Unknown")

                # Get campaign-level insights
                insights_url = f"https://graph.facebook.com/v21.0/{camp_id}/insights"
                insights_params = {
                    "access_token": META_ACCESS_TOKEN,
                    "fields": "spend,impressions,clicks,cpc,cpm,ctr,frequency,actions,action_values,cost_per_action_type",
                    "date_preset": date_preset,
                }
                insights_resp = requests.get(insights_url, params=insights_params, timeout=30)
                insights = insights_resp.json().get("data", [])

                if insights:
                    ins = insights[0]
                    # Extract purchase/conversion metrics
                    purchases = 0
                    purchase_value = 0
                    for action in ins.get("actions", []):
                        if action["action_type"] in ("purchase", "offsite_conversion.fb_pixel_purchase"):
                            purchases = int(action.get("value", 0))
                    for av in ins.get("action_values", []):
                        if av["action_type"] in ("purchase", "offsite_conversion.fb_pixel_purchase"):
                            purchase_value = float(av.get("value", 0))

                    spend = float(ins.get("spend", 0))
                    roas = round(purchase_value / spend, 2) if spend > 0 else 0

                    # Calculate CPA
                    cpa = round(spend / purchases, 2) if purchases > 0 else 0

                    campaign_info = {
                        "account": acct["name"],
                        "currency": currency,
                        "campaign": camp_name,
                        "status": camp.get("status"),
                        "objective": camp.get("objective", ""),
                        "spend": round(spend, 2),
                        "impressions": int(ins.get("impressions", 0)),
                        "clicks": int(ins.get("clicks", 0)),
                        "cpc": round(float(ins.get("cpc", 0)), 2),
                        "cpm": round(float(ins.get("cpm", 0)), 2),
                        "ctr": round(float(ins.get("ctr", 0)), 2),
                        "frequency": round(float(ins.get("frequency", 0)), 2),
                        "purchases": purchases,
                        "purchase_value": round(purchase_value, 2),
                        "roas": roas,
                        "cpa": cpa,
                    }
                    all_campaign_data.append(campaign_info)
                else:
                    # No data for this period
                    all_campaign_data.append({
                        "account": acct["name"],
                        "currency": currency,
                        "campaign": camp_name,
                        "status": camp.get("status"),
                        "objective": camp.get("objective", ""),
                        "spend": 0, "impressions": 0, "clicks": 0,
                        "cpc": 0, "cpm": 0, "ctr": 0, "frequency": 0,
                        "purchases": 0, "purchase_value": 0, "roas": 0, "cpa": 0,
                    })

        if not all_campaign_data:
            await update.message.reply_text("📭 No campaigns found across your ad accounts.")
            return

        # Quick summary before AI analysis
        total_spend = sum(c["spend"] for c in all_campaign_data)
        total_revenue = sum(c["purchase_value"] for c in all_campaign_data)
        total_purchases = sum(c["purchases"] for c in all_campaign_data)
        active_count = sum(1 for c in all_campaign_data if c["status"] == "ACTIVE")
        overall_roas = round(total_revenue / total_spend, 2) if total_spend > 0 else 0

        quick_summary = (
            f"📊 **Performance Overview** ({range_label})\n\n"
            f"💰 Total Spend: MYR {total_spend:,.2f}\n"
            f"💵 Total Revenue: MYR {total_revenue:,.2f}\n"
            f"📈 Overall ROAS: {overall_roas}x\n"
            f"🛒 Total Purchases: {total_purchases}\n"
            f"📋 Campaigns: {active_count} active, {len(all_campaign_data)} total\n\n"
            f"🤖 Analyzing with AI..."
        )
        await update.message.reply_text(quick_summary, parse_mode="Markdown")

        # Send to Claude for analysis
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        memories = load_memory()
        learnings_text = "\n".join([m.get("text", "")[:200] for m in memories[-10:]]) if memories else "No learnings yet."

        analysis_msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=3000,
            messages=[{"role": "user", "content": ANALYSIS_PROMPT.format(
                campaign_data=json.dumps(all_campaign_data, indent=2),
                learnings=learnings_text,
            )}]
        )

        analysis = analysis_msg.content[0].text.strip()

        # Split long messages (Telegram has 4096 char limit)
        if len(analysis) > 4000:
            parts = [analysis[i:i+4000] for i in range(0, len(analysis), 4000)]
            for part in parts:
                await update.message.reply_text(part)
        else:
            await update.message.reply_text(analysis)

        # Auto-learn: save performance insights to memory
        performance_learning = {
            "type": "performance_analysis",
            "date": datetime.now().strftime("%Y-%m-%d"),
            "range": range_label,
            "total_spend": total_spend,
            "overall_roas": overall_roas,
            "total_purchases": total_purchases,
            "text": f"Performance {range_label}: ROAS {overall_roas}x, {total_purchases} purchases, MYR {total_spend:.0f} spent. "
                    f"Top: {max(all_campaign_data, key=lambda x: x['roas'])['campaign'] if all_campaign_data else 'N/A'} "
                    f"(ROAS {max(all_campaign_data, key=lambda x: x['roas'])['roas'] if all_campaign_data else 0}x)",
        }
        memories = load_memory()
        # Keep only last 20 performance memories to avoid bloat
        perf_memories = [m for m in memories if m.get("type") != "performance_analysis"]
        perf_only = [m for m in memories if m.get("type") == "performance_analysis"]
        perf_only.append(performance_learning)
        perf_only = perf_only[-20:]
        save_memory(perf_memories + perf_only)

        logger.info(f"Performance analysis complete: {len(all_campaign_data)} campaigns, ROAS {overall_roas}x")

    except Exception as e:
        logger.error(f"Performance analysis failed: {e}")
        await update.message.reply_text(f"❌ Error pulling performance data: {e}")


# ─────────────────────────────────────────────
# COMPETITOR SPY — Monitor competitor ads via Ad Library API
# ─────────────────────────────────────────────

AD_LIBRARY_URL = "https://graph.facebook.com/v21.0/ads_archive"

async def cmd_spy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search Meta Ad Library for competitor ads."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("Sorry, you're not authorized.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "🕵️ **Competitor Spy**\n\n"
            "Usage: `/spy [brand name]`\n\n"
            "Examples:\n"
            "• `/spy Sol de Janeiro`\n"
            "• `/spy Lush Malaysia`\n"
            "• `/spy CeraVe`\n"
            "• `/spy Native body care`\n\n"
            "I'll search Meta Ad Library and show you their active ads, "
            "then analyze their strategy for you to learn from.",
            parse_mode="Markdown",
        )
        return

    search_term = " ".join(args)
    await update.message.reply_text(f"🔍 Searching Meta Ad Library for **{search_term}**...", parse_mode="Markdown")

    try:
        params = {
            "access_token": META_ACCESS_TOKEN,
            "search_terms": search_term,
            "ad_reached_countries": '["MY"]',
            "ad_active_status": "ACTIVE",
            "fields": "ad_creative_bodies,ad_creative_link_titles,ad_creative_link_descriptions,page_name,ad_delivery_start_time,ad_snapshot_url,spend,impressions",
            "limit": 10,
        }
        resp = requests.get(AD_LIBRARY_URL, params=params, timeout=30)
        data = resp.json()

        ads = data.get("data", [])
        if not ads:
            # Try without country filter
            params.pop("ad_reached_countries")
            resp = requests.get(AD_LIBRARY_URL, params=params, timeout=30)
            data = resp.json()
            ads = data.get("data", [])

        if not ads:
            await update.message.reply_text(
                f"📭 No active ads found for \"{search_term}\".\n\n"
                f"Tips:\n• Try the exact brand name\n• Try adding the country (e.g., \"{search_term} Malaysia\")\n"
                f"• Some brands may not be running ads right now"
            )
            return

        # Format and display ads
        ads_text = f"🕵️ **Found {len(ads)} active ads for \"{search_term}\":**\n\n"

        ads_for_analysis = []
        for i, ad in enumerate(ads[:8]):
            page_name = ad.get("page_name", "Unknown")
            bodies = ad.get("ad_creative_bodies", [])
            titles = ad.get("ad_creative_link_titles", [])
            descriptions = ad.get("ad_creative_link_descriptions", [])
            start_date = ad.get("ad_delivery_start_time", "")[:10]
            snapshot_url = ad.get("ad_snapshot_url", "")

            body_text = bodies[0][:200] if bodies else "No copy"
            title_text = titles[0][:100] if titles else "No headline"
            desc_text = descriptions[0][:100] if descriptions else ""

            ads_text += (
                f"**Ad {i+1}** — {page_name}\n"
                f"📅 Running since: {start_date}\n"
                f"📝 Copy: {body_text}\n"
                f"🏷 Headline: {title_text}\n"
            )
            if desc_text:
                ads_text += f"📄 Description: {desc_text}\n"
            if snapshot_url:
                ads_text += f"🔗 [View Ad]({snapshot_url})\n"
            ads_text += "\n"

            ads_for_analysis.append({
                "page": page_name,
                "copy": body_text,
                "headline": title_text,
                "description": desc_text,
                "running_since": start_date,
            })

        # Send ad list
        if len(ads_text) > 4000:
            parts = [ads_text[i:i+4000] for i in range(0, len(ads_text), 4000)]
            for part in parts:
                await update.message.reply_text(part, parse_mode="Markdown", disable_web_page_preview=True)
        else:
            await update.message.reply_text(ads_text, parse_mode="Markdown", disable_web_page_preview=True)

        # Analyze with Claude
        await update.message.reply_text("🤖 Analyzing competitor strategy...")

        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        spy_prompt = f"""Analyze these competitor ads from "{search_term}" found on Meta Ad Library.

COMPETITOR ADS:
{json.dumps(ads_for_analysis, indent=2)}

You are analyzing this for Lovemaya, a Malaysian personal care brand (bath gel, body lotion, perfume, hand cream).

Provide:
1. **Messaging Patterns**: What angles are they using? (benefit, social proof, urgency, discount, etc.)
2. **Copy Style**: Tone, length, language choices, CTAs
3. **What's Working**: Which ads have been running longest (= probably working)
4. **Gaps & Opportunities**: What are they NOT doing that Lovemaya could do?
5. **Steal-Worthy Ideas**: 2-3 specific creative concepts Lovemaya could adapt (not copy)
6. **Suggested Lovemaya Response**: A brief (1-2 sentences) that Lovemaya could use to counter or differentiate

Be specific and actionable. Reference actual ad copy from the data."""

        analysis_msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[{"role": "user", "content": spy_prompt}]
        )

        analysis = analysis_msg.content[0].text.strip()

        if len(analysis) > 4000:
            parts = [analysis[i:i+4000] for i in range(0, len(analysis), 4000)]
            for part in parts:
                await update.message.reply_text(part)
        else:
            await update.message.reply_text(analysis)

        # Save competitor insights to memory
        spy_learning = {
            "type": "competitor_research",
            "brand": search_term,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "ads_found": len(ads),
            "text": f"Competitor spy on {search_term} ({len(ads)} active ads found). "
                    f"Key angles: {', '.join(set(a.get('headline', '') for a in ads_for_analysis[:5]))}. "
                    f"Running since: {ads_for_analysis[0].get('running_since', 'unknown') if ads_for_analysis else 'N/A'}."
        }
        memories = load_memory()
        memories.append(spy_learning)
        save_memory(memories[-50:])  # Keep last 50 memories

        logger.info(f"Competitor spy complete: {search_term}, {len(ads)} ads found")

    except Exception as e:
        logger.error(f"Competitor spy failed: {e}")
        await update.message.reply_text(f"❌ Error searching Ad Library: {e}")


# ─────────────────────────────────────────────
# AD-LEVEL PERFORMANCE — Drill into specific campaigns
# ─────────────────────────────────────────────

async def cmd_drill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Drill into ad-level performance for a specific campaign."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("Sorry, you're not authorized.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "🔎 **Drill Into Campaign**\n\n"
            "Usage: `/drill [campaign name or keyword]`\n\n"
            "Examples:\n"
            "• `/drill bath gel`\n"
            "• `/drill PP | LM | SHOPIFY`\n"
            "• `/drill anniversary`\n\n"
            "I'll show you ad-level breakdown with metrics for each ad.",
            parse_mode="Markdown",
        )
        return

    search = " ".join(args).lower()
    await update.message.reply_text(f"🔎 Looking for campaigns matching \"{search}\"...")

    try:
        all_ad_data = []
        for acct_key, acct in AD_ACCOUNTS.items():
            acct_id = acct["id"]
            currency = acct.get("currency", "MYR")

            # Get ads with insights
            ads_url = f"https://graph.facebook.com/v21.0/{acct_id}/ads"
            params = {
                "access_token": META_ACCESS_TOKEN,
                "fields": "name,status,campaign{name},adset{name},insights.date_preset(last_7d){spend,impressions,clicks,cpc,cpm,ctr,frequency,actions,action_values}",
                "filtering": json.dumps([{"field": "campaign.name", "operator": "CONTAIN", "value": search}]),
                "limit": 30,
            }
            resp = requests.get(ads_url, params=params, timeout=30)
            data = resp.json()
            ads = data.get("data", [])

            for ad in ads:
                ins_data = ad.get("insights", {}).get("data", [])
                ins = ins_data[0] if ins_data else {}

                purchases = 0
                purchase_value = 0
                for action in ins.get("actions", []):
                    if action["action_type"] in ("purchase", "offsite_conversion.fb_pixel_purchase"):
                        purchases = int(action.get("value", 0))
                for av in ins.get("action_values", []):
                    if av["action_type"] in ("purchase", "offsite_conversion.fb_pixel_purchase"):
                        purchase_value = float(av.get("value", 0))

                spend = float(ins.get("spend", 0))
                all_ad_data.append({
                    "account": acct["name"],
                    "campaign": ad.get("campaign", {}).get("name", ""),
                    "adset": ad.get("adset", {}).get("name", ""),
                    "ad_name": ad.get("name", ""),
                    "status": ad.get("status", ""),
                    "spend": round(spend, 2),
                    "impressions": int(ins.get("impressions", 0)),
                    "clicks": int(ins.get("clicks", 0)),
                    "cpc": round(float(ins.get("cpc", 0)), 2),
                    "ctr": round(float(ins.get("ctr", 0)), 2),
                    "purchases": purchases,
                    "roas": round(purchase_value / spend, 2) if spend > 0 else 0,
                })

        if not all_ad_data:
            await update.message.reply_text(f"📭 No ads found matching \"{search}\". Try a different keyword.")
            return

        # Sort by spend (highest first)
        all_ad_data.sort(key=lambda x: x["spend"], reverse=True)

        # Format results
        result = f"🔎 **Ad Breakdown** (matching \"{search}\", last 7d)\n\n"
        for ad in all_ad_data[:15]:
            emoji = "🟢" if ad["roas"] >= 3 else "🟡" if ad["roas"] >= 1.5 else "🔴" if ad["spend"] > 50 else "👀"
            result += (
                f"{emoji} **{ad['ad_name']}**\n"
                f"   Campaign: {ad['campaign']}\n"
                f"   Spend: MYR {ad['spend']} | ROAS: {ad['roas']}x | CTR: {ad['ctr']}%\n"
                f"   Clicks: {ad['clicks']} | CPC: MYR {ad['cpc']} | Purchases: {ad['purchases']}\n\n"
            )

        result += "🟢 = Scale | 🟡 = Improve | 🔴 = Kill | 👀 = Watch"

        if len(result) > 4000:
            parts = [result[i:i+4000] for i in range(0, len(result), 4000)]
            for part in parts:
                await update.message.reply_text(part, parse_mode="Markdown")
        else:
            await update.message.reply_text(result, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Drill failed: {e}")
        await update.message.reply_text(f"❌ Error: {e}")


# Store pending strategy suggestions per user
pending_strategies = {}

STRATEGY_PROMPT = """You are a DTC performance marketing strategist for Lovemaya (Malaysian body care brand).

The user wants to create a campaign. Based on their brief, suggest a complete campaign strategy.

USER BRIEF: {brief}

BRAND LEARNINGS (from past research):
{learnings}

Based on the brief, suggest a strategy using Lovemaya's taxonomy system. Return VALID JSON ONLY (no markdown):

{{
  "strategy_summary": "1-2 sentence summary of what you recommend and why",
  "funnel": "PP or RT — with reason",
  "destination": "SHOPIFY, SHOPEE, RETAIL, or OTH — with reason",
  "biz_objective": "BAU or CAMPAIGN — with reason",
  "campaign_objective": "AWA/TRF/EGM/LEADS/SAL — with reason",
  "product_category": "BATH GEL/BODY LOTION/BODY MIST/BODY SCRUB/BUNDLE/etc.",
  "audience_suggestion": "describe the ideal target audience and why",
  "audience_setup": "A+ or ORI — with reason",
  "placement": "A+ or Manual — with reason",
  "suggested_angles": [
    {{"code": "DISCOUNT", "reason": "why this angle works for this brief"}},
    {{"code": "REVIEW", "reason": "why this angle works"}},
    {{"code": "FOMO", "reason": "why this angle works"}}
  ],
  "ad_format": "STA/VID/CAR/CLN — with reason",
  "budget_suggestion": "suggest daily budget in MYR with reasoning",
  "key_insight": "one powerful insight from brand learnings that applies here"
}}

Pick 3 angles that would work best. Reference specific brand learnings if relevant.
Today's date: {today}"""


async def handle_brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main handler — receives brief, suggests strategy, then generates campaign."""
    user_id = update.effective_user.id

    if not is_authorized(user_id):
        await update.message.reply_text("Sorry, you're not authorized to use this bot.")
        return

    brief_text = update.message.text
    logger.info(f"Brief received from {user_id}: {brief_text[:100]}...")

    # Check if this is a response to skip strategy (user typed "skip" or similar)
    if brief_text.lower().strip() in ["skip", "just create", "go ahead", "proceed"]:
        if user_id in pending_strategies:
            # User wants to skip strategy and go straight to generation
            brief_text = pending_strategies[user_id]["original_brief"]
            del pending_strategies[user_id]
            # Fall through to campaign generation below
        else:
            pass  # Normal brief, continue

    # Step 0A: Detect product from brief
    matched_products = detect_product(brief_text)

    if not matched_products and user_id not in pending_product_selection and user_id not in pending_strategies:
        # No product detected — ask user to pick one
        catalog = get_all_products_for_picker()
        if catalog:
            # Store the brief so we can resume after product selection
            pending_product_selection[user_id] = {"brief": brief_text}

            # Build product picker buttons (2 per row)
            buttons = []
            row = []
            for i, product in enumerate(catalog):
                row.append(InlineKeyboardButton(
                    f"📦 {product['name']}",
                    callback_data=f"pick_product_{product['id']}"
                ))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)
            buttons.append([InlineKeyboardButton("🔀 Mixed / All Products", callback_data="pick_product_mixed_series")])

            await update.message.reply_text(
                "🤔 I couldn't detect which product you want to promote.\n\n"
                "Which product is this ad for?",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return

    # If user came back from product selection, retrieve the stored product
    if user_id in pending_product_selection:
        stored = pending_product_selection[user_id]
        if "selected_product" in stored:
            # Product was selected via picker — enrich brief with product info
            selected = stored["selected_product"]
            brief_text = stored["brief"]
            product_images = get_product_images(selected, brief_text)
            # Tag the brief with product info so Claude and the image flow know
            brief_text += f"\n[PRODUCT: {selected['name']} | CODE: {selected['taxonomy_code']}]"
            # Store product images for later use
            context.user_data["selected_product"] = selected
            context.user_data["product_images"] = product_images
            del pending_product_selection[user_id]
        else:
            # Brief has product detected already
            del pending_product_selection[user_id]

    # If product was detected from brief text, store it
    if matched_products and "selected_product" not in context.user_data:
        best_match = matched_products[0]
        product_images = get_product_images(best_match, brief_text)
        context.user_data["selected_product"] = best_match
        context.user_data["product_images"] = product_images
        logger.info(f"Product auto-detected: {best_match['name']} ({len(product_images)} images available)")

    # Step 0B: Generate strategy suggestion first (unless brief is very detailed)
    brief_lower = brief_text.lower()
    is_detailed = len(brief_text) > 200 or "|" in brief_text  # Already has taxonomy codes
    has_pending = user_id in pending_strategies

    if not is_detailed and not has_pending:
        await update.message.reply_text("🧠 Analyzing your brief...")

        try:
            client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
            memories = load_memory()
            learnings_text = "\n".join([m.get("text", "")[:200] for m in memories[-10:]]) if memories else "No brand learnings yet."
            today = datetime.now().strftime("%B %d, %Y")

            strategy_msg = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1500,
                messages=[{"role": "user", "content": STRATEGY_PROMPT.format(
                    brief=brief_text, learnings=learnings_text, today=today
                )}]
            )

            strategy_text = strategy_msg.content[0].text.strip()
            strategy = json.loads(strategy_text)

            # Store strategy and original brief
            pending_strategies[user_id] = {
                "strategy": strategy,
                "original_brief": brief_text,
            }

            # Format strategy suggestion
            angles_text = ""
            for a in strategy.get("suggested_angles", []):
                angles_text += f"\n  • {a['code']} — {a['reason']}"

            suggestion = (
                f"💡 Strategy Suggestion:\n\n"
                f"{strategy.get('strategy_summary', '')}\n\n"
                f"📊 Recommended Setup:\n"
                f"  Funnel: {strategy.get('funnel', '?')}\n"
                f"  Objective: {strategy.get('campaign_objective', '?')}\n"
                f"  Product: {strategy.get('product_category', '?')}\n"
                f"  Audience: {strategy.get('audience_suggestion', '?')}\n"
                f"  Audience Setup: {strategy.get('audience_setup', '?')}\n"
                f"  Placement: {strategy.get('placement', '?')}\n"
                f"  Format: {strategy.get('ad_format', '?')}\n"
                f"  Budget: {strategy.get('budget_suggestion', '?')}\n\n"
                f"🎯 Suggested Angles:{angles_text}\n\n"
                f"💎 Key Insight: {strategy.get('key_insight', 'N/A')}"
            )

            keyboard = [
                [InlineKeyboardButton("✅ Approve & Create", callback_data="strategy_approve")],
                [InlineKeyboardButton("✏️ Let me adjust my brief", callback_data="strategy_adjust")],
                [InlineKeyboardButton("⏩ Skip suggestions next time", callback_data="strategy_skip")],
            ]

            await update.message.reply_text(suggestion, reply_markup=InlineKeyboardMarkup(keyboard))
            return  # Wait for user to approve

        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"Strategy suggestion failed, proceeding directly: {e}")
            # Fall through to direct campaign generation

    # If user approved strategy or brief is detailed enough, generate campaign directly
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
            ad_name = v.get("name", "")
            lang = v.get("language", "")
            lang_tag = f" ({lang})" if lang else ""
            if ad_name and "|" in ad_name:
                variants_preview += f"\n• {ad_name}\n  → {v.get('primary_text', '')}"
            else:
                variants_preview += f"\n• [{v.get('angle', '')}{lang_tag}] {v.get('primary_text', '')}"

        acct = campaign.get("_ad_account", detected_account)
        currency = acct.get("currency", "MYR")
        budget = campaign.get("adset", {}).get("daily_budget", "?")
        budget_label = "Campaign Budget (CBO)" if budget_type == "CBO" else "Ad Set Budget (ABO)"
        audience_label = "Advantage+ (AI)" if campaign.get("_audience_type") == "ADV+" else "Manual"

        # Show taxonomy names
        adset_name = campaign.get('adset', {}).get('name', 'Ad Set')
        preview_text = (
            f"✅ Campaign Ready!\n\n"
            f"📂 Account: {acct['name']}\n"
            f"📋 Campaign: {campaign.get('campaign_name', 'Campaign')}\n"
            f"📁 Ad Set: {adset_name}\n"
            f"🎯 {campaign.get('objective', 'TRAFFIC')}\n"
            f"💰 {currency} {budget} (cents)/day — {budget_label}\n"
            f"🧠 Audience: {audience_label}\n"
            f"👥 {campaign.get('adset', {}).get('gender', 'All')}, "
            f"age {campaign.get('adset', {}).get('age_min', 18)}-{campaign.get('adset', {}).get('age_max', 65)}\n"
            f"📍 {', '.join(str(l) for l in campaign.get('adset', {}).get('locations', []))}\n\n"
            f"📝 Ads:{variants_preview}\n\n"
            f"🛡 Policy: {campaign.get('policy_check', 'No issues')}"
        )

        await status_msg.edit_text(preview_text)

        # Step 5: Show product catalog images OR generate AI images
        product_images = context.user_data.get("product_images", [])
        selected_product = context.user_data.get("selected_product")

        if product_images:
            # ── USE REAL PRODUCT PHOTOS FROM CATALOG ──
            product_name = selected_product.get("name", "Product") if selected_product else "Product"
            await update.message.reply_text(f"📸 Found {len(product_images)} product image(s) for **{product_name}**. Pick one for your ad:", parse_mode="Markdown")

            pending_images[user_id] = {
                "paths": product_images,
                "prompts": [f"Product photo: {product_name}"] * len(product_images),
            }

            for idx, path in enumerate(product_images[:6]):
                try:
                    with open(path, "rb") as photo:
                        await update.message.reply_photo(
                            photo=photo,
                            caption=f"📸 {product_name} — Image {idx + 1}",
                            reply_markup=InlineKeyboardMarkup([[
                                InlineKeyboardButton(f"✅ Use Image {idx + 1}", callback_data=f"pick_img_{idx}"),
                            ]]),
                        )
                except Exception as img_err:
                    logger.error(f"Failed to send catalog image {idx}: {img_err}")

            await update.message.reply_text(
                "👆 Pick a product image above, or choose an action:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎨 Generate AI image instead", callback_data="gen_ai_images")],
                    [InlineKeyboardButton("🚀 Create without image", callback_data="exec_api")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="exec_cancel")],
                ]),
            )
        else:
            # ── NO CATALOG IMAGES — GENERATE WITH AI ──
            image_prompts = campaign.get("image_prompts", [])
            if not image_prompts and campaign.get("image_prompt"):
                image_prompts = [campaign["image_prompt"]]

            if (HIGGSFIELD_API_KEY or TOGETHER_API_KEY) and image_prompts:
                await update.message.reply_text("🎨 No product images in catalog. Generating AI images... (~30 seconds)")

                generated_paths = generate_multiple_images(image_prompts[:3])
                valid_images = [(i, p) for i, p in enumerate(generated_paths) if p]

                if valid_images:
                    pending_images[user_id] = {
                        "paths": generated_paths,
                        "prompts": image_prompts,
                    }

                    for idx, path in valid_images:
                        try:
                            with open(path, "rb") as photo:
                                await update.message.reply_photo(
                                    photo=photo,
                                    caption=f"🖼 AI Image {idx + 1}: {image_prompts[idx][:150]}...",
                                    reply_markup=InlineKeyboardMarkup([[
                                        InlineKeyboardButton(f"✅ Use Image {idx + 1}", callback_data=f"pick_img_{idx}"),
                                    ]]),
                                )
                        except Exception as img_err:
                            logger.error(f"Failed to send image {idx}: {img_err}")

                    await update.message.reply_text(
                        "👆 Pick an image above, or choose an action:",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🚀 Create without image", callback_data="exec_api")],
                            [InlineKeyboardButton("❌ Cancel", callback_data="exec_cancel")],
                        ]),
                    )
                else:
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
                # No image API keys and no catalog images — show normal approval buttons
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
    """Handle button clicks for campaign approval and strategy suggestions."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    action = query.data

    # ── PRODUCT PICKER CALLBACK ──
    if action.startswith("pick_product_"):
        product_id = action.replace("pick_product_", "")
        stored = pending_product_selection.get(user_id)
        if not stored:
            await query.edit_message_text("⚠️ Session expired. Send your brief again.")
            return

        # Find the selected product from catalog
        catalog = load_product_catalog()
        selected = None
        for p in catalog:
            if p["id"] == product_id:
                selected = p
                break

        if not selected:
            await query.edit_message_text("⚠️ Product not found. Send your brief again.")
            return

        # Store selection and re-trigger the brief handler
        pending_product_selection[user_id]["selected_product"] = selected
        product_images = get_product_images(selected, stored["brief"])
        img_count = len(product_images)

        await query.edit_message_text(
            f"✅ Got it — **{selected['name']}**\n"
            f"📸 {img_count} product image{'s' if img_count != 1 else ''} available in catalog\n\n"
            f"Processing your brief...",
            parse_mode="Markdown",
        )

        # Re-trigger the brief handler with the original brief text
        # We create a fake-ish flow by calling handle_brief logic
        original_brief = stored["brief"]
        # We need to simulate a message — use query.message.reply_text to continue
        # Instead, we manually invoke the flow:
        context.user_data["selected_product"] = selected
        context.user_data["product_images"] = product_images

        # Build the enriched brief
        enriched_brief = original_brief + f"\n[PRODUCT: {selected['name']} | CODE: {selected['taxonomy_code']}]"

        # Continue to strategy suggestion
        try:
            client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
            memories = load_memory()
            learnings_text = "\n".join([m.get("text", "")[:200] for m in memories[-10:]]) if memories else "No brand learnings yet."
            today = datetime.now().strftime("%B %d, %Y")

            strategy_msg = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1500,
                messages=[{"role": "user", "content": STRATEGY_PROMPT.format(
                    brief=enriched_brief, learnings=learnings_text, today=today
                )}]
            )

            strategy_text = strategy_msg.content[0].text.strip()
            strategy = json.loads(strategy_text)

            pending_strategies[user_id] = {
                "strategy": strategy,
                "original_brief": enriched_brief,
            }

            # Clean up product selection
            if user_id in pending_product_selection:
                del pending_product_selection[user_id]

            # Format strategy suggestion
            angles_text = ""
            for a in strategy.get("suggested_angles", []):
                angles_text += f"\n  • {a['code']} — {a['reason']}"

            suggestion = (
                f"💡 Strategy Suggestion:\n\n"
                f"{strategy.get('strategy_summary', '')}\n\n"
                f"📊 Recommended Setup:\n"
                f"  Product: {selected['name']}\n"
                f"  Funnel: {strategy.get('funnel', '?')}\n"
                f"  Objective: {strategy.get('campaign_objective', '?')}\n"
                f"  Audience: {strategy.get('audience_suggestion', '?')}\n"
                f"  Audience Setup: {strategy.get('audience_setup', '?')}\n"
                f"  Placement: {strategy.get('placement', '?')}\n"
                f"  Ad Format: {strategy.get('ad_format', '?')}\n"
                f"  Angles: {angles_text}\n\n"
                f"💵 Budget: {strategy.get('budget_suggestion', '?')}\n"
                f"💡 Reason: {strategy.get('strategy_reason', '')}"
            )

            await query.message.reply_text(
                suggestion,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Approve & Create", callback_data="strategy_approve")],
                    [InlineKeyboardButton("✏️ Let me adjust my brief", callback_data="strategy_adjust")],
                    [InlineKeyboardButton("⏩ Skip suggestions next time", callback_data="strategy_skip")],
                ]),
            )
        except Exception as e:
            logger.error(f"Strategy generation failed after product pick: {e}")
            await query.message.reply_text(f"⚠️ Strategy suggestion failed: {e}\n\nSend your brief again to retry.")
        return

    # ── STRATEGY SUGGESTION CALLBACKS ──
    if action == "strategy_approve":
        strategy_data = pending_strategies.get(user_id)
        if not strategy_data:
            await query.edit_message_text("⚠️ Strategy expired. Send your brief again.")
            return
        original_brief = strategy_data["original_brief"]
        strategy = strategy_data["strategy"]
        del pending_strategies[user_id]

        # Enrich the brief with strategy suggestions before generating
        enriched_brief = (
            f"{original_brief}\n\n"
            f"[STRATEGY APPROVED — use these settings]\n"
            f"Funnel: {strategy.get('funnel', 'PP').split(' ')[0]}\n"
            f"Campaign Objective: {strategy.get('campaign_objective', 'SAL').split(' ')[0]}\n"
            f"Product: {strategy.get('product_category', '')}\n"
            f"Audience: {strategy.get('audience_suggestion', '')}\n"
            f"Audience Setup: {strategy.get('audience_setup', 'A+').split(' ')[0]}\n"
            f"Placement: {strategy.get('placement', 'A+').split(' ')[0]}\n"
            f"Ad Format: {strategy.get('ad_format', 'STA').split(' ')[0]}\n"
            f"Angles: {', '.join([a['code'] for a in strategy.get('suggested_angles', [])])}\n"
            f"Budget: {strategy.get('budget_suggestion', 'MYR 30/day')}"
        )

        await query.edit_message_text("✅ Strategy approved! Generating campaign...")

        # Re-detect product images (in case context.user_data was lost)
        if not context.user_data.get("product_images"):
            re_matched = detect_product(original_brief)
            if re_matched:
                best = re_matched[0]
                re_images = get_product_images(best, original_brief)
                context.user_data["selected_product"] = best
                context.user_data["product_images"] = re_images
                logger.info(f"Strategy approve: re-detected {best['name']} ({len(re_images)} images)")

        # Now generate campaign with enriched brief
        try:
            detected_account = detect_ad_account(original_brief)
            budget_type = detect_budget_type(original_brief)
            audience_type = detect_audience_type(original_brief)

            campaign = generate_campaign_with_claude(enriched_brief)
            logger.info(f"Campaign generated from strategy: {campaign.get('campaign_name')}")

            account_key = campaign.get("ad_account", DEFAULT_AD_ACCOUNT)
            if account_key in AD_ACCOUNTS:
                campaign["_ad_account"] = AD_ACCOUNTS[account_key]
            else:
                campaign["_ad_account"] = detected_account
            campaign["_budget_type"] = budget_type
            campaign["_audience_type"] = audience_type

            pending_campaigns[user_id] = campaign

            # Build preview
            variants_preview = ""
            for v in campaign.get("ad_variants", []):
                ad_name = v.get("name", "")
                if ad_name and "|" in ad_name:
                    variants_preview += f"\n• {ad_name}\n  → {v.get('primary_text', '')}"
                else:
                    lang = v.get("language", "")
                    variants_preview += f"\n• [{v.get('angle', '')} ({lang})] {v.get('primary_text', '')}"

            acct = campaign.get("_ad_account", detected_account)
            adset_name = campaign.get('adset', {}).get('name', 'Ad Set')
            preview_text = (
                f"✅ Campaign Ready!\n\n"
                f"📂 Account: {acct['name']}\n"
                f"📋 Campaign: {campaign.get('campaign_name', 'Campaign')}\n"
                f"📁 Ad Set: {adset_name}\n"
                f"🎯 {campaign.get('objective', 'TRAFFIC')}\n"
                f"💰 {acct.get('currency', 'MYR')} {campaign.get('adset', {}).get('daily_budget', '?')} (cents)/day\n\n"
                f"📝 Ads:{variants_preview}\n\n"
                f"🛡 Policy: {campaign.get('policy_check', 'No issues')}"
            )

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
            await query.message.reply_text(preview_text)

            # Step: Show product catalog images OR generate AI images
            product_images = context.user_data.get("product_images", [])
            selected_product = context.user_data.get("selected_product")

            if product_images:
                # ── USE REAL PRODUCT PHOTOS FROM CATALOG ──
                product_name = selected_product.get("name", "Product") if selected_product else "Product"
                await query.message.reply_text(f"📸 Found {len(product_images)} product image(s) for **{product_name}**. Pick one for your ad:", parse_mode="Markdown")

                pending_images[user_id] = {
                    "paths": product_images,
                    "prompts": [f"Product photo: {product_name}"] * len(product_images),
                }

                for idx, path in enumerate(product_images[:6]):
                    try:
                        with open(path, "rb") as photo:
                            await query.message.reply_photo(
                                photo=photo,
                                caption=f"📸 {product_name} — Image {idx + 1}",
                                reply_markup=InlineKeyboardMarkup([[
                                    InlineKeyboardButton(f"✅ Use Image {idx + 1}", callback_data=f"pick_img_{idx}"),
                                ]]),
                            )
                    except Exception as img_err:
                        logger.error(f"Failed to send catalog image {idx}: {img_err}")

                await query.message.reply_text(
                    "👆 Pick a product image above, or choose an action:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🎨 Generate AI image instead", callback_data="gen_ai_images")],
                        [InlineKeyboardButton("🚀 Create without image", callback_data="exec_api")],
                        [InlineKeyboardButton("❌ Cancel", callback_data="exec_cancel")],
                    ]),
                )
            else:
                # ── NO CATALOG IMAGES — GENERATE WITH AI ──
                image_prompts = campaign.get("image_prompts", [])
                if not image_prompts and campaign.get("image_prompt"):
                    image_prompts = [campaign["image_prompt"]]

                if (HIGGSFIELD_API_KEY or TOGETHER_API_KEY) and image_prompts:
                    await query.message.reply_text("🎨 No product images in catalog. Generating AI images... (~30 seconds)")

                    generated_paths = generate_multiple_images(image_prompts[:3])
                    valid_images = [(i, p) for i, p in enumerate(generated_paths) if p]

                    if valid_images:
                        pending_images[user_id] = {
                            "paths": generated_paths,
                            "prompts": image_prompts,
                        }

                        for idx, path in valid_images:
                            try:
                                with open(path, "rb") as photo:
                                    await query.message.reply_photo(
                                        photo=photo,
                                        caption=f"🖼 AI Image {idx + 1}: {image_prompts[idx][:150]}...",
                                        reply_markup=InlineKeyboardMarkup([[
                                            InlineKeyboardButton(f"✅ Use Image {idx + 1}", callback_data=f"pick_img_{idx}"),
                                        ]]),
                                    )
                            except Exception as img_err:
                                logger.error(f"Failed to send image {idx}: {img_err}")

                        await query.message.reply_text(
                            "👆 Pick an image above, or choose an action:",
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("🚀 Create without image", callback_data="exec_api")],
                                [InlineKeyboardButton("❌ Cancel", callback_data="exec_cancel")],
                            ]),
                        )
                    else:
                        await query.message.reply_text(
                            "⚠️ Image generation failed. You can still create the campaign:",
                            reply_markup=InlineKeyboardMarkup(keyboard),
                        )
                else:
                    await query.message.reply_text("What should I do?", reply_markup=InlineKeyboardMarkup(keyboard))

        except Exception as e:
            await query.message.reply_text(f"❌ Error generating campaign: {e}")
        return

    elif action == "strategy_adjust":
        await query.edit_message_text(
            "✏️ No problem! Send me an updated brief with more details.\n\n"
            "You can include specifics like:\n"
            "• Product: bath gel / body mist / bundle\n"
            "• Angle: discount / FOMO / review\n"
            "• Audience: women 25-45 / retarget past buyers\n"
            "• Budget: MYR 50/day\n"
            "• Language: EN, BM, CHI"
        )
        if user_id in pending_strategies:
            del pending_strategies[user_id]
        return

    elif action == "strategy_skip":
        if user_id in pending_strategies:
            original_brief = pending_strategies[user_id]["original_brief"]
            del pending_strategies[user_id]
            await query.edit_message_text("⏩ Skipping suggestions, generating campaign directly...")
            # Re-trigger handle_brief with the original message
            # Store a flag so it skips strategy next time
            context.user_data["skip_strategy"] = True
            # Create a fake message-like flow by directly generating
            detected_account = detect_ad_account(original_brief)
            campaign = generate_campaign_with_claude(original_brief)
            account_key = campaign.get("ad_account", DEFAULT_AD_ACCOUNT)
            if account_key in AD_ACCOUNTS:
                campaign["_ad_account"] = AD_ACCOUNTS[account_key]
            else:
                campaign["_ad_account"] = detected_account
            campaign["_budget_type"] = "CBO"
            campaign["_audience_type"] = detect_audience_type(original_brief)
            pending_campaigns[user_id] = campaign

            adset_name = campaign.get('adset', {}).get('name', 'Ad Set')
            acct = campaign["_ad_account"]
            await query.message.reply_text(
                f"✅ Campaign Ready!\n\n"
                f"📋 Campaign: {campaign.get('campaign_name', 'Campaign')}\n"
                f"📁 Ad Set: {adset_name}\n"
                f"🎯 {campaign.get('objective', 'TRAFFIC')}"
            )
            keyboard = [
                [InlineKeyboardButton("🚀 Create via API", callback_data="exec_api")],
                [InlineKeyboardButton("❌ Cancel", callback_data="exec_cancel")],
            ]
            await query.message.reply_text("What should I do?", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # ── EXISTING CAMPAIGN CALLBACKS ──
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

    # ── GENERATE AI IMAGES (user chose AI over catalog images) ──
    if action == "gen_ai_images":
        campaign = pending_campaigns.get(user_id)
        if not campaign:
            await query.edit_message_text("⚠️ No pending campaign. Send a new brief.")
            return

        image_prompts = campaign.get("image_prompts", [])
        if not image_prompts and campaign.get("image_prompt"):
            image_prompts = [campaign["image_prompt"]]

        if (HIGGSFIELD_API_KEY or TOGETHER_API_KEY) and image_prompts:
            await query.edit_message_text("🎨 Generating AI images... (this takes ~30 seconds)")

            generated_paths = generate_multiple_images(image_prompts[:3])
            valid_images = [(i, p) for i, p in enumerate(generated_paths) if p]

            if valid_images:
                pending_images[user_id] = {
                    "paths": generated_paths,
                    "prompts": image_prompts,
                }

                for idx, path in valid_images:
                    try:
                        with open(path, "rb") as photo:
                            await query.message.reply_photo(
                                photo=photo,
                                caption=f"🖼 AI Image {idx + 1}: {image_prompts[idx][:150]}...",
                                reply_markup=InlineKeyboardMarkup([[
                                    InlineKeyboardButton(f"✅ Use Image {idx + 1}", callback_data=f"pick_img_{idx}"),
                                ]]),
                            )
                    except Exception as img_err:
                        logger.error(f"Failed to send AI image {idx}: {img_err}")

                await query.message.reply_text(
                    "👆 Pick an image above, or:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🚀 Create without image", callback_data="exec_api")],
                        [InlineKeyboardButton("❌ Cancel", callback_data="exec_cancel")],
                    ]),
                )
            else:
                await query.message.reply_text(
                    "⚠️ AI image generation failed. You can still create the campaign:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🚀 Create via API", callback_data="exec_api")],
                        [InlineKeyboardButton("❌ Cancel", callback_data="exec_cancel")],
                    ]),
                )
        else:
            await query.message.reply_text(
                "⚠️ No image generation API configured (need HIGGSFIELD_API_KEY or TOGETHER_API_KEY).",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🚀 Create via API", callback_data="exec_api")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="exec_cancel")],
                ]),
            )
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
            debug_obj = result.get("debug_resolved_objective", campaign.get("objective", "?"))
            debug_opt = result.get("debug_optimization_goal", "?")
            debug_dest = result.get("debug_destination_type", "?")
            debug_pixel = "yes" if META_PIXEL_ID else "no"
            debug_step = result.get("failed_step", "?")
            debug_cid = result.get("campaign_id", "none")
            msg = (
                f"❌ Campaign creation failed ({BOT_VERSION}):\n\n"
                f"{errors_text}\n\n"
                f"🔍 Debug:\n"
                f"  step={debug_step}\n"
                f"  campaign_id={debug_cid}\n"
                f"  obj={debug_obj} opt={debug_opt} dest={debug_dest}\n"
                f"  pixel={debug_pixel}\n"
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
    print(f"  LOVEMAYA ADS BOT {BOT_VERSION} — Starting")
    print(f"  Meta API: {'Configured' if META_ACCESS_TOKEN else 'Not configured'}")
    print(f"  Pixel ID: {META_PIXEL_ID if META_PIXEL_ID else 'NOT SET — Sales campaigns will downgrade to Traffic'}")
    print(f"  Page ID: {META_PAGE_ID if META_PAGE_ID else 'Will auto-detect'}")
    print(f"  IG Actor: {META_IG_ACTOR_ID if META_IG_ACTOR_ID else 'Will auto-detect'}")
    print(f"  Allowed users: {ALLOWED_USER_IDS or 'All (no restriction)'}")
    print("=" * 50)

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("ideas", cmd_ideas))
    app.add_handler(CommandHandler("learn", cmd_learn))
    app.add_handler(CommandHandler("funnel", cmd_funnel))
    app.add_handler(CommandHandler("feedback", cmd_feedback))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("performance", cmd_performance))
    app.add_handler(CommandHandler("report", cmd_performance))  # Alias
    app.add_handler(CommandHandler("spy", cmd_spy))
    app.add_handler(CommandHandler("drill", cmd_drill))
    app.add_handler(CallbackQueryHandler(handle_approval))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_analysis))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_brief))

    print("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
