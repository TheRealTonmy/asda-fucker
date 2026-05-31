import discord
from discord.ext import commands
from discord import app_commands
import requests
import io
import barcode as pybarcode
from barcode.writer import ImageWriter
from PIL import Image, ImageDraw, ImageFont
import textwrap
import sqlite3
import datetime
import math
import logging
import os
import asyncio
import urllib.request
from dotenv import load_dotenv
from typing import List, Dict, Optional, Any

# --- SETUP & SECURITY ---
# Force absolute paths so tmux never gets confused about where it is running
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ALGOLIA_APP_ID = os.getenv("ALGOLIA_APP_ID")
ALGOLIA_API_KEY = os.getenv("ALGOLIA_API_KEY")

SEARCH_STORE_ID = os.getenv("SEARCH_STORE_ID", "4565")
BARCODE_STORE_ID = int(os.getenv("BARCODE_STORE_ID", "6416"))

session = requests.Session()
product_cache: Dict[str, List[Dict[str, Any]]] = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ==========================================
# AUTO FONT DOWNLOADER
# ==========================================
FONT_BOLD = os.path.join(BASE_DIR, "OpenSans-Bold.ttf")
FONT_REGULAR = os.path.join(BASE_DIR, "OpenSans-Regular.ttf")

def ensure_fonts():
    fonts = {
        FONT_BOLD: "https://raw.githubusercontent.com/googlefonts/opensans/main/fonts/ttf/OpenSans-Bold.ttf",
        FONT_REGULAR: "https://raw.githubusercontent.com/googlefonts/opensans/main/fonts/ttf/OpenSans-Regular.ttf"
    }
    for font_path, url in fonts.items():
        if not os.path.exists(font_path):
            logging.info(f"Downloading missing font: {os.path.basename(font_path)}...")
            try:
                urllib.request.urlretrieve(url, font_path)
            except Exception as e:
                logging.error(f"Failed to download font: {e}")

# Run this immediately when the script starts
ensure_fonts()

# ==========================================
# DATABASE FUNCTIONS 
# ==========================================
def init_db() -> None:
    db_path = os.path.join(BASE_DIR, 'scans.db')
    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS logs
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT, product TEXT, ean TEXT, new_price TEXT, timestamp TEXT)''')
        conn.commit()

def add_log(user: str, product: str, ean: str, new_price: str) -> None:
    db_path = os.path.join(BASE_DIR, 'scans.db')
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO logs (user, product, ean, new_price, timestamp) VALUES (?, ?, ?, ?, ?)",
                  (user, product, ean, new_price, timestamp))
        conn.commit()

# ==========================================
# BARCODE & BEAUTIFIED IMAGE FUNCTIONS
# ==========================================
def validate_ean(ean: str) -> bool:
    return bool(ean) and ean.isdigit() and len(ean) == 13

def calculate_check_digit(base_string: str) -> str:
    reversed_payload = base_string[::-1]
    luhn_sum = sum((int(char) * 2 - 9 if int(char) * 2 >= 10 else int(char) * 2) if i % 2 == 0 else int(char) for i, char in enumerate(reversed_payload))
    return str((10 - (luhn_sum % 10)) % 10)

def build_barcode(ean13: str, old_price_pence: int, new_price_pence: int, store_code: int) -> str:
    base_string = f"{old_price_pence:03d}{ean13}{new_price_pence:05d}{store_code:04d}"
    return base_string + calculate_check_digit(base_string)

def create_digital_label(code_string: str, product_name: str, new_price_str: str, old_price_str: str, image_url: Optional[str], filename: str = "label.png") -> discord.File:
    # Start with an overly tall canvas so we never run out of room
    canvas_w, max_canvas_h = 800, 2000
    canvas = Image.new('RGB', (canvas_w, max_canvas_h), '#F8F9FA') 
    draw = ImageDraw.Draw(canvas)

    try:
        font_banner = ImageFont.truetype(FONT_BOLD, 50)
        font_title = ImageFont.truetype(FONT_REGULAR, 40)
        font_was = ImageFont.truetype(FONT_REGULAR, 36)
        font_price = ImageFont.truetype(FONT_BOLD, 95) 
    except IOError as e:
        logging.error(f"CRITICAL FONT ERROR: {e}")
        font_banner = font_title = font_was = font_price = ImageFont.load_default()

    # Dynamic Red Header
    draw.rectangle([0, 0, canvas_w, 80], fill="#E5202B")
    draw.text((canvas_w / 2, 40), "CLEARANCE", fill="white", font=font_banner, anchor="mm", weight="bold")
    current_y = 110

    # Image Processing
    if image_url:
        try:
            resp = session.get(image_url, stream=True, timeout=10)
            if resp.status_code == 200:
                prod_img = Image.open(resp.raw).convert("RGBA")
                prod_img.thumbnail((350, 350), Image.Resampling.LANCZOS)
                
                img_x = int((canvas_w - prod_img.width) / 2)
                padding = 12
                draw.rounded_rectangle(
                    [img_x - padding, current_y - padding, img_x + prod_img.width + padding, current_y + prod_img.height + padding], 
                    radius=15, fill="white", outline="#D0D5DD", width=2
                )
                
                canvas.paste(prod_img, (img_x, current_y), prod_img)
                current_y += prod_img.height + 60
        except Exception as e:
            logging.error(f"Image load failed: {e}")

    # Centered Title
    wrapped_title = textwrap.fill(product_name, width=32)
    draw.multiline_text((canvas_w / 2, current_y), wrapped_title, fill="#222222", font=font_title, anchor="ma", align="center", spacing=10)
    text_bbox = draw.multiline_textbbox((canvas_w / 2, current_y), wrapped_title, font=font_title, anchor="ma", spacing=10)
    current_y = text_bbox[3] + 30

    # WAS Price with strike-through
    was_text = f"WAS {old_price_str}"
    was_bbox = draw.textbbox((canvas_w / 2, current_y), was_text, font=font_was, anchor="mm")
    draw.text((canvas_w / 2, current_y), was_text, fill="#888888", font=font_was, anchor="mm")
    draw.line([was_bbox[0] - 15, current_y, was_bbox[2] + 15, current_y], fill="#E5202B", width=4)
    current_y += 45

    # NOW Price Box
    box_w, box_h = 400, 140
    box_x = int((canvas_w - box_w) / 2)
    
    draw.rounded_rectangle([box_x, current_y, box_x + box_w, current_y + box_h], radius=20, fill="#FFCC00", outline="#E5B700", width=3)
    draw.text((canvas_w / 2, current_y + (box_h / 2)), new_price_str, fill="#111827", font=font_price, anchor="mm")
    current_y += box_h + 50

    # Barcode Generation
    rv = io.BytesIO()
    pybarcode.get('code128', code_string, writer=ImageWriter()).write(
        rv, 
        options={
            "write_text": False,  
            "module_height": 20.0, 
            "quiet_zone": 2.0
        }
    )
    rv.seek(0)
    
    bc_img = Image.open(rv).convert("RGBA")
    if bc_img.width > canvas_w - 80:
        bc_img.thumbnail((canvas_w - 80, 220), Image.Resampling.LANCZOS)
    canvas.paste(bc_img, (int((canvas_w - bc_img.width) / 2), current_y), bc_img)

    # Calculate exact bottom position and dynamically crop the canvas
    # This guarantees a 60px bottom margin every single time
    final_h = current_y + bc_img.height + 60
    final_canvas = canvas.crop((0, 0, canvas_w, int(final_h)))

    final_buffer = io.BytesIO()
    final_canvas.save(final_buffer, format="PNG")
    final_buffer.seek(0)
    return discord.File(fp=final_buffer, filename=filename)

def search_asda_products(search_term: str) -> List[Dict[str, Any]]:
    clean_term = search_term.strip().lower()
    if clean_term in product_cache:
        return product_cache[clean_term]

    url = f"https://{ALGOLIA_APP_ID.lower()}-dsn.algolia.net/1/indexes/*/queries"
    payload = {"requests": [{"indexName": "ASDA_PRODUCTS", "query": search_term, "hitsPerPage": 25, "filters": f"(STATUS:A OR STATUS:I) AND NOT DISPLAY_ONLINE:false AND NOT UNTRAITED_STORES:{SEARCH_STORE_ID} AND STOCK.{SEARCH_STORE_ID} > 0 AND NOT PRODUCT_TYPE:Bundle"}]}
    
    try:
        response = session.post(url, headers={"User-Agent": "Mozilla/5.0"}, json=payload, params={"x-algolia-application-id": ALGOLIA_APP_ID, "x-algolia-api-key": ALGOLIA_API_KEY}, timeout=10)
        response.raise_for_status()
        hits = response.json()['results'][0].get('hits', [])
        
        results = []
        for item in hits:
            image_id = item.get('IMAGE_ID')
            raw_ean = str(image_id if image_id else item.get('ID'))
            clean_ean = ''.join(char for char in raw_ean if char.isdigit())
            ean = clean_ean.zfill(13)[:13] if clean_ean else "0000000000000"
            
            raw_prices = item.get('PRICES', {})
            region_data = raw_prices.get('EN') or next(iter(raw_prices.values()), None) if isinstance(raw_prices, dict) else None
            
            raw_price_pence = None
            price_str = "Price unavailable"
            
            if isinstance(region_data, dict) and region_data.get('PRICE') is not None:
                raw_val = float(region_data.get('PRICE'))
                price_str = f"£{raw_val:.2f}"
                raw_price_pence = int(raw_val * 100) 
            
            prod_name = item.get('NAME')
            if not prod_name:
                prod_name = "Unknown Item"
                
            results.append({
                "name": prod_name,
                "price": price_str,
                "raw_price_pence": raw_price_pence,
                "ean": ean, 
                "image_url": f"https://asdagroceries.scene7.com/is/image/asdagroceries/{image_id}" if image_id else None
            })
            
        product_cache[clean_term] = results
        return results
    except Exception as e:
        logging.error(f"Algolia search failed: {e}")
        return []

# ==========================================
# DISCORD UI COMPONENTS
# ==========================================
def create_label_embed(product: Dict, display_price: str, item: Dict, alternate: bool = False) -> discord.Embed:
    title = "✅ Markdown Label Generated"
    if alternate: title += " (Alternate Version)"
    
    embed = discord.Embed(title=title, color=0xE51937)
    embed.set_author(name="Retail Pricing System", icon_url="https://cdn-icons-png.flaticon.com/512/2956/2956869.png")
    
    if product['image_url']:
        embed.set_thumbnail(url=product['image_url'])
        
    embed.add_field(name="Product", value=f"**{product['name']}**", inline=False)
    embed.add_field(name="Original", value=f"~~{item['old_price_str']}~~", inline=True)
    embed.add_field(name="Clearance", value=f"**{display_price}**", inline=True)
    embed.add_field(name="Item EAN", value=f"`{product['ean']}`", inline=True)
    
    embed.add_field(name="Scan Code String", value=f"```ini\n[{item['barcode']}]\n```", inline=False)
    
    embed.set_footer(text="Store Operations • Ready to print")
    embed.timestamp = discord.utils.utcnow()
    return embed

async def generate_and_send_labels(interaction: discord.Interaction, product: Dict, target_price_pence: int, old_price_pence: Optional[int] = None):
    if not validate_ean(product['ean']):
        await interaction.followup.send("❌ Cannot generate label: Invalid EAN detected.", ephemeral=True)
        return

    barcodes_to_generate = []
    
    if target_price_pence == 50 or old_price_pence == 330 or old_price_pence == 510:
        barcodes_to_generate.append({"old_price_str": "£5.10", "barcode": build_barcode(product['ean'], 510, target_price_pence, BARCODE_STORE_ID)})
        barcodes_to_generate.append({"old_price_str": "£3.30", "barcode": build_barcode(product['ean'], 330, target_price_pence, BARCODE_STORE_ID)})
    else:
        base = old_price_pence if old_price_pence else product.get('raw_price_pence', 400)
        barcode_old_price = base if base <= 999 else 330
        barcodes_to_generate.append({"old_price_str": f"£{base / 100:.2f}", "barcode": build_barcode(product['ean'], barcode_old_price, target_price_pence, BARCODE_STORE_ID)})

    display_price = f"£{target_price_pence / 100:.2f}"
    primary_item = barcodes_to_generate[0]

    embed = create_label_embed(product, display_price, primary_item)
    file_name = f"clearance_{primary_item['old_price_str'].replace('£', '').replace('.', '')}.png"
    
    image_file = await asyncio.to_thread(
        create_digital_label, primary_item['barcode'], product['name'], display_price, primary_item['old_price_str'], product['image_url'], filename=file_name
    )

    add_log(interaction.user.name, product['name'], product['ean'], display_price)

    alt_item = barcodes_to_generate[1] if len(barcodes_to_generate) > 1 else None
    view = FinalLabelView(product, target_price_pence, primary_item, alt_item)

    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, file=image_file, view=view)
    else:
        await interaction.response.send_message(embed=embed, file=image_file, view=view)

class FinalLabelView(discord.ui.View):
    def __init__(self, product: Dict, target_price_pence: int, primary_item: Dict, alt_item: Optional[Dict] = None):
        super().__init__(timeout=None)
        self.product = product
        self.target_price_pence = target_price_pence
        self.primary_item = primary_item
        self.alt_item = alt_item
        if not self.alt_item: self.remove_item(self.btn_regen)

    @discord.ui.button(label="Send to DM", style=discord.ButtonStyle.secondary, emoji="📬")
    async def btn_dm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        display_price = f"£{self.target_price_pence / 100:.2f}"
        embed = create_label_embed(self.product, display_price, self.primary_item)
        file_name = f"clearance_dm.png"
        
        image_file = await asyncio.to_thread(create_digital_label, self.primary_item['barcode'], self.product['name'], display_price, self.primary_item['old_price_str'], self.product['image_url'], filename=file_name)
        
        try:
            await interaction.user.send(embed=embed, file=image_file)
            await interaction.followup.send("✅ Sent to your DMs!", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("❌ DMs are closed.", ephemeral=True)

    @discord.ui.button(label="Regenerate Alt. Barcode", style=discord.ButtonStyle.primary, emoji="🔄")
    async def btn_regen(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        display_price = f"£{self.target_price_pence / 100:.2f}"
        embed = create_label_embed(self.product, display_price, self.alt_item, alternate=True)
        file_name = f"clearance_alt.png"
        
        image_file = await asyncio.to_thread(create_digital_label, self.alt_item['barcode'], self.product['name'], display_price, self.alt_item['old_price_str'], self.product['image_url'], filename=file_name)
        
        button.disabled = True
        await interaction.message.edit(view=self)
        await interaction.followup.send(embed=embed, file=image_file, view=FinalLabelView(self.product, self.target_price_pence, self.alt_item))

class PriceInputModal(discord.ui.Modal, title='Manual Price Override'):
    new_price = discord.ui.TextInput(label='New Price (in pence, e.g. 85 for 85p)', placeholder="Enter numbers only...", required=True)
    def __init__(self, selected_product: Dict):
        super().__init__()
        self.product = selected_product

    async def on_submit(self, interaction: discord.Interaction):
        try:
            target = int(self.new_price.value)
            await interaction.response.defer()
            await generate_and_send_labels(interaction, self.product, target_price_pence=target, old_price_pence=330)
        except ValueError:
            await interaction.response.send_message("❌ Error: Must be a whole number.", ephemeral=True)

class ActionButtons(discord.ui.View):
    def __init__(self, product: Dict):
        super().__init__()
        self.product = product

    async def check_price(self, interaction: discord.Interaction) -> bool:
        if not self.product.get('raw_price_pence'):
            await interaction.response.send_message("❌ Cannot Auto-Calculate: No live price found.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Auto 50% Off", style=discord.ButtonStyle.blurple, emoji="📉", row=0)
    async def btn_auto(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self.check_price(interaction):
            await interaction.response.defer()
            half_price = int(self.product['raw_price_pence'] / 2)
            await generate_and_send_labels(interaction, self.product, target_price_pence=half_price, old_price_pence=self.product['raw_price_pence'])

    @discord.ui.button(label="Auto 75% Off", style=discord.ButtonStyle.blurple, emoji="✂️", row=0)
    async def btn_75p(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self.check_price(interaction):
            await interaction.response.defer()
            drop_75 = math.ceil((self.product['raw_price_pence'] * 0.25) / 10.0) * 10
            await generate_and_send_labels(interaction, self.product, target_price_pence=drop_75, old_price_pence=self.product['raw_price_pence'])

    @discord.ui.button(label="Clearance (80% Off)", style=discord.ButtonStyle.danger, emoji="🔥", row=1)
    async def btn_80p(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self.check_price(interaction):
            await interaction.response.defer()
            max_drop = math.ceil((self.product['raw_price_pence'] * 0.20) / 10.0) * 10
            await generate_and_send_labels(interaction, self.product, target_price_pence=max_drop, old_price_pence=self.product['raw_price_pence'])

    @discord.ui.button(label="Drop to 50p", style=discord.ButtonStyle.danger, emoji="🪙", row=1)
    async def btn_50p(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await generate_and_send_labels(interaction, self.product, target_price_pence=50)

    @discord.ui.button(label="Custom Price Input", style=discord.ButtonStyle.success, emoji="✏️", row=2)
    async def btn_custom(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PriceInputModal(self.product))

    @discord.ui.button(label="Vanity Markdown", style=discord.ButtonStyle.secondary, emoji="✨", row=2)
    async def btn_vanity(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self.check_price(interaction):
            await interaction.response.defer()
            p = self.product['raw_price_pence'] / 100.0
            w = math.floor(p)
            t = math.floor(10 * (p - w))
            m_pounds = (w / 10.0) + ((t + 1) / 100.0)
            vanity_pence = int(round(m_pounds * 100))
            await generate_and_send_labels(interaction, self.product, target_price_pence=vanity_pence, old_price_pence=330)

class ProductSelect(discord.ui.Select):
    def __init__(self, products: List[Dict]):
        options = [discord.SelectOption(label=p['name'][:90], description=f"Price: {p['price']} | EAN: {p['ean']}", emoji="🛒", value=str(i)) for i, p in enumerate(products[:25])]
        super().__init__(placeholder="Select the target product...", min_values=1, max_values=1, options=options)
        self.products = products

    async def callback(self, interaction: discord.Interaction):
        selected = self.products[int(self.values[0])]
        view = ActionButtons(selected)
        
        embed = discord.Embed(title="🛍️ Product Confirmed", description="Please select an exact markdown strategy below.", color=0xFFCC00)
        embed.add_field(name="Item Name", value=f"**{selected['name']}**", inline=False)
        embed.add_field(name="Live System Price", value=f"`{selected['price']}`", inline=True)
        embed.add_field(name="EAN", value=f"`{selected['ean']}`", inline=True)
        
        if selected['image_url']:
            embed.set_thumbnail(url=selected['image_url'])
            
        await interaction.response.edit_message(content=None, embed=embed, view=view)

class ProductView(discord.ui.View):
    def __init__(self, products: List[Dict]):
        super().__init__()
        self.add_item(ProductSelect(products))

# ==========================================
# BOT INIT
# ==========================================
class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        init_db() 
        await self.tree.sync() 
        logging.info("Store Operations Bot Online & Synced")

bot = Bot()

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    logging.error(f"Command Error: {error}")
    msg = "❌ An unexpected error occurred while processing this request."
    if not interaction.response.is_done(): await interaction.response.send_message(msg, ephemeral=True)
    else: await interaction.followup.send(msg, ephemeral=True)

@bot.tree.command(name="barcode", description="Search inventory database and generate a retail clearance label")
@app_commands.checks.cooldown(1, 5.0)
async def barcode(interaction: discord.Interaction, search_query: str):
    await interaction.response.defer(ephemeral=False)
    products = search_asda_products(search_query)
    
    if not products:
        await interaction.followup.send(f"❌ No live products found matching `{search_query}`.")
        return
        
    embed = discord.Embed(title="🔍 Inventory Search Results", description=f"Found **{len(products)}** items matching **'{search_query}'**.\nPlease select the exact shelf match from the dropdown.", color=0x2563EB)
    await interaction.followup.send(embed=embed, view=ProductView(products))

@bot.tree.command(name="logs", description="Audit log of recent clearance labels generated")
async def show_logs(interaction: discord.Interaction):
    db_path = os.path.join(BASE_DIR, 'scans.db')
    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        c.execute("SELECT user, product, ean, new_price, timestamp FROM logs ORDER BY id DESC LIMIT 5")
        logs = c.fetchall()

    if not logs:
        await interaction.response.send_message("📭 No operational logs generated yet.", ephemeral=True)
        return

    embed = discord.Embed(title="📜 Recent Markdowns", color=0x9CA3AF)
    for log in logs:
        user, prod, ean, price, ts = log
        embed.add_field(name=f"Markdown to {price}", value=f"**Item:** {prod[:40]}...\n**EAN:** `{ean}`\n**User:** {user} at {ts}", inline=False)

    await interaction.response.send_message(embed=embed)

if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)