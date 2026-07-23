import asyncio
import logging
import os
import uuid
import secrets
import string
from datetime import datetime, timedelta
from typing import Optional, List, Any
from io import BytesIO

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile, LabeledPrice, PreCheckoutQuery,
    ReplyKeyboardRemove
)
import asyncpg
from asyncpg import Pool, Record

from PIL import Image, ImageDraw, ImageFont
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.units import inch, mm

import qrcode

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]
DATABASE_URL = os.getenv("DATABASE_URL")
PROVIDER_TOKEN_TEST = os.getenv("PROVIDER_TOKEN_TEST")
PROVIDER_TOKEN_LIVE = os.getenv("PROVIDER_TOKEN_LIVE")
BOT_USERNAME = os.getenv("BOT_USERNAME")

if not all([BOT_TOKEN, DATABASE_URL, PROVIDER_TOKEN_LIVE]):
    raise ValueError("Не все переменные окружения заданы! Нужны: BOT_TOKEN, DATABASE_URL, PROVIDER_TOKEN_LIVE")
if not PROVIDER_TOKEN_TEST:
    PROVIDER_TOKEN_TEST = PROVIDER_TOKEN_LIVE  # если тестовый не задан, используем боевой (не рекомендуется)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)

db_pool: Optional[Pool] = None

def get_provider_token(user_id: int, choice: str = None) -> str:
    """
    Возвращает токен провайдера в зависимости от пользователя и выбора.
    Если пользователь админ и choice передан, используем choice.
    Иначе для всех остальных – боевой токен.
    """
    if user_id in ADMIN_IDS and choice:
        if choice == "test":
            return PROVIDER_TOKEN_TEST
        elif choice == "live":
            return PROVIDER_TOKEN_LIVE
    return PROVIDER_TOKEN_LIVE

async def is_payments_enabled() -> bool:
    """Возвращает True, если приём платежей включён для клиентов."""
    val = await get_setting('payments_enabled', 'true')
    return val.lower() == 'true'

async def create_tables():
    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                tg_id BIGINT UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(tg_id),
                status TEXT DEFAULT 'pending',
                birth_date TEXT,
                birth_time TEXT,
                birth_city TEXT,
                gender TEXT,
                email TEXT,
                file_path TEXT,
                file_id TEXT,
                file_name TEXT,
                total_price INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                gift_code TEXT,
                gift_amount_used INTEGER DEFAULT 0,
                client_name TEXT
            )
        ''')
        await conn.execute('ALTER TABLE orders ADD COLUMN IF NOT EXISTS gift_code TEXT')
        await conn.execute('ALTER TABLE orders ADD COLUMN IF NOT EXISTS gift_amount_used INTEGER DEFAULT 0')
        await conn.execute('ALTER TABLE orders ADD COLUMN IF NOT EXISTS total_price INTEGER DEFAULT 0')
        await conn.execute('ALTER TABLE orders ADD COLUMN IF NOT EXISTS client_name TEXT')
        await conn.execute('ALTER TABLE orders ADD COLUMN IF NOT EXISTS file_id TEXT')
        await conn.execute('ALTER TABLE orders ADD COLUMN IF NOT EXISTS file_name TEXT')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                order_id INTEGER REFERENCES orders(id),
                user_id BIGINT,
                amount INTEGER,
                payload TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        default_settings = {
            'price': '3000',
            'cert_x': '400',
            'cert_y': '300',
            'cert_font_size': '60',
            'cert_font_color': '0,0,0',
            'cert_font_path': 'fonts/arial.ttf',
            'cert_template_default': 'media/templates/certificate_default.png',
            'cert_code_prefix': 'GIFT-',
            'cert_code_length': '8',
            'send_pdf_enabled': 'true',
            'qr_enabled': 'true',
            'qr_x': '700',
            'qr_y': '450',
            'qr_size': '150',
            'qr_image_enabled': 'true',
            'qr_image_x': '700',
            'qr_image_y': '450',
            'qr_image_size': '150',
            'qr_pdf_enabled': 'true',
            'qr_pdf_x': '300',
            'qr_pdf_y': '400',
            'qr_pdf_size': '200',
            'qr_pdf_label': 'Отсканируйте для активации',
            'qr_pdf_label_font_size': '14',
            'payments_enabled': 'true'
        }
        for key, val in default_settings.items():
            await conn.execute('''
                INSERT INTO settings (key, value) VALUES ($1, $2)
                ON CONFLICT (key) DO NOTHING
            ''', key, val)
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS gift_certificates (
                id SERIAL PRIMARY KEY,
                code TEXT UNIQUE NOT NULL,
                purchaser_id BIGINT NOT NULL REFERENCES users(tg_id),
                recipient_id BIGINT DEFAULT NULL REFERENCES users(tg_id),
                amount INTEGER NOT NULL,
                balance INTEGER NOT NULL,
                status TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                used_at TIMESTAMP,
                expires_at TIMESTAMP,
                order_id INTEGER DEFAULT NULL,
                gift_message TEXT
            )
        ''')
        await conn.execute('ALTER TABLE gift_certificates ADD COLUMN IF NOT EXISTS gift_message TEXT')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS gift_templates (
                id SERIAL PRIMARY KEY,
                amount INTEGER NOT NULL UNIQUE,
                file_path TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS fonts (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_gift_code ON gift_certificates(code)')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_users_tg_id ON users(tg_id)')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id)')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)')
    logger.info("Таблицы созданы/проверены")

async def drop_tables():
    async with db_pool.acquire() as conn:
        await conn.execute('DROP TABLE IF EXISTS payments CASCADE')
        await conn.execute('DROP TABLE IF EXISTS orders CASCADE')
        await conn.execute('DROP TABLE IF EXISTS users CASCADE')
        await conn.execute('DROP TABLE IF EXISTS settings CASCADE')
        await conn.execute('DROP TABLE IF EXISTS gift_certificates CASCADE')
        await conn.execute('DROP TABLE IF EXISTS gift_templates CASCADE')
        await conn.execute('DROP TABLE IF EXISTS fonts CASCADE')
    logger.info("Таблицы удалены")

async def init_db_pool():
    global db_pool
    db_pool = await asyncpg.create_pool(
        dsn=DATABASE_URL,
        min_size=1,
        max_size=10,
        command_timeout=60
    )
    logger.info("Пул соединений с PostgreSQL создан")
    await create_tables()

async def close_db_pool():
    if db_pool:
        await db_pool.close()
        logger.info("Пул соединений закрыт")

async def get_setting(key: str, default: str = None) -> Optional[str]:
    async with db_pool.acquire() as conn:
        row = await conn.fetchval('SELECT value FROM settings WHERE key = $1', key)
        return row if row else default

async def set_setting(key: str, value: str) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO settings (key, value) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = $2
        ''', key, value)

async def get_price() -> int:
    price_str = await get_setting('price', '3000')
    try:
        price = int(price_str.strip())
        if price <= 0:
            return 3000
        return price
    except (ValueError, AttributeError):
        return 3000

async def get_send_pdf_enabled() -> bool:
    val = await get_setting('send_pdf_enabled', 'true')
    return val.lower() == 'true'

async def set_send_pdf_enabled(enabled: bool) -> None:
    await set_setting('send_pdf_enabled', 'true' if enabled else 'false')

async def get_template_path(amount: int) -> Optional[str]:
    async with db_pool.acquire() as conn:
        row = await conn.fetchval('SELECT file_path FROM gift_templates WHERE amount = $1', amount)
        return row

async def add_template(amount: int, file_path: str) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO gift_templates (amount, file_path) VALUES ($1, $2)
            ON CONFLICT (amount) DO UPDATE SET file_path = $2, updated_at = CURRENT_TIMESTAMP
        ''', amount, file_path)

async def delete_template(amount: int) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute('DELETE FROM gift_templates WHERE amount = $1', amount)

async def get_all_templates() -> List[Record]:
    async with db_pool.acquire() as conn:
        return await conn.fetch('SELECT * FROM gift_templates ORDER BY amount')

async def get_fonts() -> List[Record]:
    async with db_pool.acquire() as conn:
        return await conn.fetch('SELECT * FROM fonts ORDER BY created_at DESC')

async def add_font(name: str, file_path: str) -> int:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow('''
            INSERT INTO fonts (name, file_path) VALUES ($1, $2)
            RETURNING id
        ''', name, file_path)
        return row['id']

async def delete_font(font_id: int) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute('DELETE FROM fonts WHERE id = $1', font_id)

async def get_font_by_id(font_id: int) -> Optional[Record]:
    async with db_pool.acquire() as conn:
        return await conn.fetchrow('SELECT * FROM fonts WHERE id = $1', font_id)

async def get_last_font() -> Optional[Record]:
    async with db_pool.acquire() as conn:
        return await conn.fetchrow('SELECT * FROM fonts ORDER BY created_at DESC LIMIT 1')

async def set_active_font(font_path: str):
    await set_setting('cert_font_path', font_path)

async def get_qr_image_settings():
    enabled = await get_setting('qr_image_enabled', 'true')
    x = int(await get_setting('qr_image_x', '700'))
    y = int(await get_setting('qr_image_y', '450'))
    size = int(await get_setting('qr_image_size', '150'))
    return {
        'enabled': enabled.lower() == 'true',
        'x': x,
        'y': y,
        'size': size
    }

async def get_qr_pdf_settings():
    enabled = await get_setting('qr_pdf_enabled', 'true')
    x = int(await get_setting('qr_pdf_x', '300'))
    y = int(await get_setting('qr_pdf_y', '400'))
    size = int(await get_setting('qr_pdf_size', '200'))
    label = await get_setting('qr_pdf_label', 'Отсканируйте для активации')
    label_font_size = int(await get_setting('qr_pdf_label_font_size', '14'))
    return {
        'enabled': enabled.lower() == 'true',
        'x': x,
        'y': y,
        'size': size,
        'label': label,
        'label_font_size': label_font_size
    }

async def set_qr_image_enabled(enabled: bool):
    await set_setting('qr_image_enabled', 'true' if enabled else 'false')

async def set_qr_image_x(x: int):
    await set_setting('qr_image_x', str(x))

async def set_qr_image_y(y: int):
    await set_setting('qr_image_y', str(y))

async def set_qr_image_size(size: int):
    await set_setting('qr_image_size', str(size))

async def set_qr_pdf_enabled(enabled: bool):
    await set_setting('qr_pdf_enabled', 'true' if enabled else 'false')

async def set_qr_pdf_x(x: int):
    await set_setting('qr_pdf_x', str(x))

async def set_qr_pdf_y(y: int):
    await set_setting('qr_pdf_y', str(y))

async def set_qr_pdf_size(size: int):
    await set_setting('qr_pdf_size', str(size))

async def set_qr_pdf_label(label: str):
    await set_setting('qr_pdf_label', label)

async def set_qr_pdf_label_font_size(size: int):
    await set_setting('qr_pdf_label_font_size', str(size))

def generate_gift_code(prefix='GIFT-', length=8):
    alphabet = string.ascii_uppercase + string.digits
    return prefix + ''.join(secrets.choice(alphabet) for _ in range(length))

async def get_cert_settings():
    x = int(await get_setting('cert_x', '400'))
    y = int(await get_setting('cert_y', '300'))
    font_size = int(await get_setting('cert_font_size', '60'))
    color_str = await get_setting('cert_font_color', '0,0,0')
    font_color = tuple(map(int, color_str.split(',')))
    font_path = await get_setting('cert_font_path', 'fonts/arial.ttf')
    default_template = await get_setting('cert_template_default', 'media/templates/certificate_default.png')
    prefix = await get_setting('cert_code_prefix', 'GIFT-')
    length = int(await get_setting('cert_code_length', '8'))
    return {
        'x': x, 'y': y,
        'font_size': font_size,
        'font_color': font_color,
        'font_path': font_path,
        'default_template': default_template,
        'prefix': prefix,
        'length': length
    }

def generate_qr_code(data: str, size: int = 150) -> Image.Image:
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=10,
            border=2,
        )
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert('RGB')
        img = img.resize((size, size), Image.Resampling.LANCZOS)
        return img
    except Exception as e:
        logger.error(f"Ошибка генерации QR-кода: {e}")
        return Image.new('RGB', (size, size), color='white')

def generate_certificate_image(code: str, template_path: str, output_path: str,
                               text_position, font_path, font_size, font_color,
                               qr_image_settings: dict):
    try:
        img = Image.open(template_path).convert('RGB')
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype(font_path, font_size)
        except IOError as e:
            logger.warning(f"Не удалось загрузить шрифт {font_path}: {e}. Используется стандартный шрифт.")
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), code, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        x = text_position[0] - text_width // 2
        y = text_position[1] - text_height // 2
        draw.text((x, y), code, font=font, fill=font_color)

        if qr_image_settings.get('enabled', False):
            gift_link = f"https://t.me/{BOT_USERNAME}?start=gift_{code}"
            qr_img = generate_qr_code(gift_link, size=qr_image_settings.get('size', 150))
            qr_x = qr_image_settings.get('x', 700)
            qr_y = qr_image_settings.get('y', 450)
            img.paste(qr_img, (qr_x, qr_y))

        img.save(output_path, 'PNG')
        return True
    except Exception as e:
        logger.error(f"Ошибка генерации изображения: {e}")
        return False

def generate_certificate_pdf(code: str, image_path: str, output_path: str,
                             text_position, font_path, font_size, font_color,
                             qr_pdf_settings: dict):
    try:
        from PIL import Image as PILImage
        pil_img = PILImage.open(image_path)
        img_width_px, img_height_px = pil_img.size
        dpi = 300
        width_pts = img_width_px / dpi * 72
        height_pts = img_height_px / dpi * 72

        c = canvas.Canvas(output_path, pagesize=(width_pts, height_pts))
        c.drawImage(image_path, 0, 0, width=width_pts, height=height_pts, preserveAspectRatio=False)

        if qr_pdf_settings.get('enabled', False):
            c.showPage()
            scale = 2
            page_width = width_pts * scale
            page_height = height_pts * scale
            c.setPageSize((page_width, page_height))

            qr_size_pts = qr_pdf_settings.get('size', 200)
            qr_x_center = qr_pdf_settings.get('x', page_width/2)
            qr_y_center = qr_pdf_settings.get('y', page_height/2)

            gift_link = f"https://t.me/{BOT_USERNAME}?start=gift_{code}"
            qr_img = generate_qr_code(gift_link, size=qr_size_pts)
            temp_qr_path = f"media/temp_qr_{uuid.uuid4().hex}.png"
            qr_img.save(temp_qr_path, 'PNG')

            qr_width = qr_size_pts
            qr_height = qr_size_pts
            x_left = qr_x_center - qr_width/2
            y_bottom = qr_y_center - qr_height/2
            c.drawImage(temp_qr_path, x_left, y_bottom, width=qr_width, height=qr_height, preserveAspectRatio=True)

            label = qr_pdf_settings.get('label', 'Отсканируйте для активации')
            label_font_size = qr_pdf_settings.get('label_font_size', 14)
            try:
                pdfmetrics.registerFont(TTFont('CustomFont', font_path))
                c.setFont('CustomFont', label_font_size)
                font_name = 'CustomFont'
            except:
                c.setFont('Helvetica', label_font_size)
                font_name = 'Helvetica'
            c.setFillColorRGB(0, 0, 0)
            text_width = c.stringWidth(label, font_name, label_font_size)
            label_x = page_width/2 - text_width/2
            label_y = y_bottom - 10
            c.drawString(label_x, label_y, label)

            if os.path.exists(temp_qr_path):
                os.remove(temp_qr_path)

        c.save()
        return True
    except Exception as e:
        logger.error(f"Ошибка генерации PDF: {e}")
        return False

async def generate_certificate_files(code: str, amount: int) -> tuple:
    settings = await get_cert_settings()
    template_path = await get_template_path(amount)
    if not template_path or not os.path.exists(template_path):
        template_path = settings['default_template']
        if not os.path.exists(template_path):
            os.makedirs('media/templates', exist_ok=True)
            template_path = 'media/templates/certificate_default.png'
            img = Image.new('RGB', (800, 600), color=(255, 255, 255))
            draw = ImageDraw.Draw(img)
            draw.text((100, 100), "Подарочный сертификат", fill=(0,0,0))
            img.save(template_path)
            await set_setting('cert_template_default', template_path)
    os.makedirs('media/certificates', exist_ok=True)
    image_path = f'media/certificates/cert_{code}.png'
    pdf_path = f'media/certificates/cert_{code}.pdf'
    pos = (settings['x'], settings['y'])
    font_path = settings['font_path']
    if not os.path.exists(font_path):
        logger.warning(f"Файл шрифта не найден: {font_path}. Используется стандартный шрифт.")
        font_path = None

    qr_image_settings = await get_qr_image_settings()
    qr_pdf_settings = await get_qr_pdf_settings()

    img_ok = generate_certificate_image(
        code, template_path, image_path,
        text_position=pos,
        font_path=font_path,
        font_size=settings['font_size'],
        font_color=settings['font_color'],
        qr_image_settings=qr_image_settings
    )

    pdf_ok = False
    if await get_send_pdf_enabled():
        pdf_ok = generate_certificate_pdf(
            code, image_path, pdf_path,
            text_position=pos,
            font_path=font_path if font_path else 'Helvetica',
            font_size=settings['font_size'],
            font_color=settings['font_color'],
            qr_pdf_settings=qr_pdf_settings
        )
    if img_ok:
        return image_path, pdf_path if pdf_ok else None
    else:
        raise Exception("Ошибка генерации изображения сертификата")

# ================== НОВАЯ ФУНКЦИЯ ДЛЯ СОЗДАНИЯ ВРЕМЕННОЙ ЗАПИСИ СЕРТИФИКАТА ==================
async def create_pending_gift_certificate(purchaser_id: int, amount: int, gift_message: str = None) -> int:
    """Создаёт запись сертификата со статусом pending и возвращает её id."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow('''
            INSERT INTO gift_certificates (code, purchaser_id, amount, balance, status, gift_message)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
        ''', 'PENDING', purchaser_id, amount, 0, 'pending', gift_message)
        return row['id']

async def create_gift_certificate(purchaser_id: int, amount: int, recipient_id: int = None) -> dict:
    prefix = await get_setting('cert_code_prefix', 'GIFT-')
    length = int(await get_setting('cert_code_length', '8'))
    while True:
        code = generate_gift_code(prefix=prefix, length=length)
        async with db_pool.acquire() as conn:
            existing = await conn.fetchval('SELECT id FROM gift_certificates WHERE code = $1', code)
            if not existing:
                break
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow('''
            INSERT INTO gift_certificates (code, purchaser_id, recipient_id, amount, balance)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id, code, amount, balance
        ''', code, purchaser_id, recipient_id, amount, amount)
        return dict(row)

async def get_gift_certificate_by_code(code: str) -> Optional[Record]:
    async with db_pool.acquire() as conn:
        return await conn.fetchrow('SELECT * FROM gift_certificates WHERE code = $1', code)

async def update_gift_balance_and_status(code: str, used_amount: int, order_id: int, used_by_id: int) -> None:
    async with db_pool.acquire() as conn:
        cert = await conn.fetchrow('SELECT balance FROM gift_certificates WHERE code = $1', code)
        if not cert:
            return
        new_balance = cert['balance'] - used_amount
        if new_balance <= 0:
            await conn.execute('''
                UPDATE gift_certificates
                SET balance = 0, status = 'used', used_at = CURRENT_TIMESTAMP, order_id = $2
                WHERE code = $1
            ''', code, order_id)
        else:
            await conn.execute('''
                UPDATE gift_certificates
                SET balance = $2, order_id = $3
                WHERE code = $1
            ''', code, new_balance, order_id)

async def restore_gift_certificate(code: str, amount_to_restore: int) -> None:
    async with db_pool.acquire() as conn:
        cert = await conn.fetchrow('SELECT balance FROM gift_certificates WHERE code = $1', code)
        if not cert:
            logger.warning(f"Попытка восстановить несуществующий сертификат {code}")
            return
        new_balance = cert['balance'] + amount_to_restore
        new_status = 'active' if new_balance > 0 else 'used'
        await conn.execute('''
            UPDATE gift_certificates
            SET balance = $1, status = $2, order_id = NULL, used_at = NULL
            WHERE code = $3
        ''', new_balance, new_status, code)
        logger.info(f"Восстановлен баланс сертификата {code}: +{amount_to_restore}, новый баланс {new_balance}")

async def get_gift_certificate_by_id(cert_id: int) -> Optional[Record]:
    async with db_pool.acquire() as conn:
        return await conn.fetchrow('SELECT * FROM gift_certificates WHERE id = $1', cert_id)

async def delete_gift_certificate_by_id(cert_id: int) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow('SELECT code FROM gift_certificates WHERE id = $1', cert_id)
        if not row:
            return False
        code = row['code']
        await conn.execute('DELETE FROM gift_certificates WHERE id = $1', cert_id)
        image_path = f'media/certificates/cert_{code}.png'
        pdf_path = f'media/certificates/cert_{code}.pdf'
        for path in [image_path, pdf_path]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception as e:
                    logger.warning(f"Не удалось удалить файл {path}: {e}")
        return True

async def delete_gift_certificates_by_status(status: str) -> int:
    async with db_pool.acquire() as conn:
        if status == 'all':
            rows = await conn.fetch('SELECT id FROM gift_certificates')
        else:
            rows = await conn.fetch('SELECT id FROM gift_certificates WHERE LOWER(status) = LOWER($1)', status)
        ids = [row['id'] for row in rows]
        deleted = 0
        for cert_id in ids:
            if await delete_gift_certificate_by_id(cert_id):
                deleted += 1
        return deleted

async def get_user(tg_id: int) -> Optional[Record]:
    async with db_pool.acquire() as conn:
        return await conn.fetchrow('SELECT * FROM users WHERE tg_id = $1', tg_id)

async def create_user(tg_id: int, username: str, first_name: str, last_name: str) -> bool:
    async with db_pool.acquire() as conn:
        try:
            await conn.execute('''
                INSERT INTO users (tg_id, username, first_name, last_name)
                VALUES ($1, $2, $3, $4)
            ''', tg_id, username, first_name, last_name)
            return True
        except asyncpg.UniqueViolationError:
            return False

async def ensure_user_exists(tg_id: int, username: str = None, first_name: str = None, last_name: str = None) -> bool:
    user = await get_user(tg_id)
    if user:
        return True
    return await create_user(tg_id, username, first_name, last_name)

async def create_order(
    user_id: int,
    birth_date: str,
    birth_time: str,
    birth_city: str,
    gender: str,
    email: Optional[str],
    client_name: str,
    total_price: int = 0,
    gift_code: Optional[str] = None,
    gift_amount_used: int = 0
) -> int:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow('''
            INSERT INTO orders (user_id, status, birth_date, birth_time, birth_city, gender, email, client_name, total_price, gift_code, gift_amount_used)
            VALUES ($1, 'pending', $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING id
        ''', user_id, birth_date, birth_time, birth_city, gender, email, client_name, total_price, gift_code, gift_amount_used)
        return row['id']

async def update_order_status(order_id: int, status: str) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute('''
            UPDATE orders SET status = $1, updated_at = CURRENT_TIMESTAMP
            WHERE id = $2
        ''', status, order_id)

async def clear_order_gift(order_id: int) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute('''
            UPDATE orders SET gift_code = NULL, gift_amount_used = 0 WHERE id = $1
        ''', order_id)

async def get_order(order_id: int) -> Optional[Record]:
    async with db_pool.acquire() as conn:
        return await conn.fetchrow('SELECT * FROM orders WHERE id = $1', order_id)

async def get_orders_by_user(user_id: int) -> List[Record]:
    async with db_pool.acquire() as conn:
        return await conn.fetch('SELECT * FROM orders WHERE user_id = $1 ORDER BY created_at DESC', user_id)

async def get_active_orders_for_user(user_id: int) -> List[Record]:
    async with db_pool.acquire() as conn:
        return await conn.fetch('''
            SELECT * FROM orders WHERE user_id = $1 AND status NOT IN ('done', 'cancelled')
            ORDER BY created_at DESC
        ''', user_id)

async def get_orders_by_status(status: str) -> List[Record]:
    async with db_pool.acquire() as conn:
        return await conn.fetch('SELECT * FROM orders WHERE status = $1 ORDER BY created_at DESC', status)

async def get_pending_orders_with_gift_code(user_id: int, gift_code: str) -> List[Record]:
    async with db_pool.acquire() as conn:
        return await conn.fetch('''
            SELECT * FROM orders WHERE user_id = $1 AND gift_code = $2 AND status IN ('pending', 'pending_payment')
        ''', user_id, gift_code)

async def save_order_file(order_id: int, file_id: str, file_name: str = None) -> None:
    async with db_pool.acquire() as conn:
        if file_name:
            await conn.execute('''
                UPDATE orders SET file_id = $1, file_name = $2, file_path = NULL, status = 'done', updated_at = CURRENT_TIMESTAMP
                WHERE id = $3
            ''', file_id, file_name, order_id)
        else:
            await conn.execute('''
                UPDATE orders SET file_id = $1, file_path = NULL, status = 'done', updated_at = CURRENT_TIMESTAMP
                WHERE id = $2
            ''', file_id, order_id)

async def save_payment_history(order_id: int, user_id: int, amount: int, payload: str) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO payments (order_id, user_id, amount, payload)
            VALUES ($1, $2, $3, $4)
        ''', order_id, user_id, amount, payload)

async def anonymize_order(order_id: int) -> bool:
    try:
        async with db_pool.acquire() as conn:
            result = await conn.execute('''
                UPDATE orders
                SET 
                    birth_date = NULL,
                    birth_time = NULL,
                    birth_city = NULL,
                    gender = NULL,
                    email = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = $1 AND status = 'done'
            ''', order_id)
            if result == "UPDATE 0":
                logger.warning(f"Заказ {order_id} не найден или не в статусе 'done' для анонимизации")
                return False
            logger.info(f"Заказ {order_id} анонимизирован (персональные данные удалены)")
            return True
    except Exception as e:
        logger.error(f"Ошибка анонимизации заказа {order_id}: {e}")
        return False

class OrderStates(StatesGroup):
    waiting_birth_date = State()
    waiting_birth_time = State()
    waiting_city = State()
    waiting_gender = State()
    waiting_name = State()
    waiting_email = State()
    confirm_summary = State()
    editing_field = State()
    editing_value = State()
    waiting_gift_code_input = State()
    confirm_order_final = State()
    waiting_upload_order_id = State()
    waiting_broadcast_content = State()
    waiting_new_price = State()
    waiting_gift_amount = State()
    waiting_gift_confirm = State()
    admin_waiting_template_amount = State()
    admin_waiting_template_file = State()
    admin_waiting_confirm_delete = State()
    admin_waiting_setting_key = State()
    admin_waiting_setting_value = State()
    admin_waiting_test_amount = State()
    admin_waiting_font_file = State()
    admin_waiting_file_upload = State()
    waiting_gift_message_choice = State()
    waiting_gift_message_text = State()
    waiting_gift_message_confirm = State()
    admin_waiting_delete_cert = State()
    waiting_payment_choice = State()

class AdminStates(StatesGroup):
    waiting_reset_confirm1 = State()
    waiting_reset_confirm2 = State()

def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Заказать разбор")],
            [KeyboardButton(text="🎁 Подарочный сертификат"), KeyboardButton(text="📋 Мои заказы")],
            [KeyboardButton(text="ℹ️ О сервисе"), KeyboardButton(text="📞 Контакты")]
        ],
        resize_keyboard=True
    )

def get_admin_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⏳ Новые заявки")],
            [KeyboardButton(text="📋 Текущий заказ"), KeyboardButton(text="📋 Все заявки")],
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="📢 Рассылка")],
            [KeyboardButton(text="💰 Изменить цену"), KeyboardButton(text="⚙️ Настройки платежей")],
            [KeyboardButton(text="🎁 Сертификаты"), KeyboardButton(text="👤 Панель клиента")]
        ],
        resize_keyboard=True
    )

def get_certificates_menu_keyboard():
    keyboard = [
        [KeyboardButton(text="🎨 Шаблоны сертификатов")],
        [KeyboardButton(text="⚙️ Настройки сертификатов")],
        [KeyboardButton(text="📱 Настройки QR")],
        [KeyboardButton(text="🧪 Тестовый сертификат")],
        [KeyboardButton(text="📋 Сертификаты")],
        [KeyboardButton(text="⚙️ Настройки PDF")],
        [KeyboardButton(text="🗑️ Удалить сертификат")],
        [KeyboardButton(text="🔙 Назад")]
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

def get_order_confirm_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить и оплатить", callback_data="confirm_order")],
            [InlineKeyboardButton(text="✏️ Изменить данные", callback_data="edit_data")]
        ]
    )

def get_skip_email_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⏭️ Пропустить")]],
        resize_keyboard=True
    )

def get_status_button_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Проверить статус", callback_data="check_status")]
        ]
    )

def get_active_order_choice_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Создать ещё один заказ", callback_data="create_new_order")],
            [InlineKeyboardButton(text="📊 Проверить статус", callback_data="check_status")]
        ]
    )

def get_take_order_keyboard(order_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Взять в работу", callback_data=f"take_{order_id}")]
        ]
    )

def get_gift_amount_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="1000 ₽", callback_data="gift_1000"),
             InlineKeyboardButton(text="2000 ₽", callback_data="gift_2000")],
            [InlineKeyboardButton(text="3000 ₽", callback_data="gift_3000")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_gift")]
        ]
    )

def get_gift_code_choice_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Ввести код сертификата", callback_data="enter_gift_code")],
            [InlineKeyboardButton(text="⏭️ Пропустить", callback_data="skip_gift_code")]
        ]
    )

def get_gift_message_choice_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Написать текст", callback_data="write_gift_message")],
            [InlineKeyboardButton(text="⏭️ Пропустить", callback_data="skip_gift_message")]
        ]
    )

def get_gift_message_confirm_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_gift_message")],
            [InlineKeyboardButton(text="✏️ Изменить", callback_data="edit_gift_message")]
        ]
    )

def get_payment_choice_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🧪 Тестовый платеж", callback_data="pay_choice_test"),
                InlineKeyboardButton(text="💳 Реальный платеж", callback_data="pay_choice_live")
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="pay_choice_cancel")]
        ]
    )

def get_home_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🏠 В начало")]],
        resize_keyboard=True
    )

def get_back_home_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔙 Назад")],
            [KeyboardButton(text="🏠 В начало")]
        ],
        resize_keyboard=True
    )

def get_gender_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👨 Мужской"), KeyboardButton(text="👩 Женский")],
            [KeyboardButton(text="🔙 Назад")],
            [KeyboardButton(text="🏠 В начало")]
        ],
        resize_keyboard=True
    )

def get_email_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⏭️ Пропустить")],
            [KeyboardButton(text="🔙 Назад")],
            [KeyboardButton(text="🏠 В начало")]
        ],
        resize_keyboard=True
    )

# ============================================================
# НОВАЯ ФУНКЦИЯ ДЛЯ ПОКАЗА НАСТРОЕК ПЛАТЕЖЕЙ (Inline)
# ============================================================
async def show_payment_settings(target, edit: bool = False):
    """
    Показывает или обновляет сообщение с настройками платежей.
    target – сообщение или callback.
    edit – если True, редактирует существующее сообщение (для callback).
    """
    enabled = await is_payments_enabled()
    price = await get_price()
    status_text = "✅ ВКЛЮЧЕН" if enabled else "❌ ВЫКЛЮЧЕН"
    status_emoji = "🟢" if enabled else "🔴"
    
    text = (
        f"⚙️ Настройки платежей\n\n"
        f"Статус приёма платежей: {status_emoji} {status_text}\n"
        f"Текущая цена разбора: {price} ₽\n\n"
        f"Выберите действие:"
    )
    
    toggle_text = "🔘 Выключить платежи" if enabled else "🔘 Включить платежи"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_text, callback_data="payment_toggle")],
            [InlineKeyboardButton(text="🔍 Проверить токены", callback_data="payment_check_tokens")],
            [InlineKeyboardButton(text="💰 Изменить цену", callback_data="payment_change_price")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="payment_back")]
        ]
    )
    
    if edit and hasattr(target, 'message'):
        await target.message.edit_text(text, reply_markup=keyboard)
    elif edit and hasattr(target, 'edit_text'):
        await target.edit_text(text, reply_markup=keyboard)
    else:
        await target.answer(text, reply_markup=keyboard)

# ============================================================
# ОБРАБОТЧИКИ
# ============================================================

@dp.message(F.text.in_(["🏠 В начало", "Отмена", "Выход"]))
async def home_handler(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if user_id == bot.id:
        return
    if user_id in ADMIN_IDS:
        await message.answer("Возврат в панель администратора.", reply_markup=get_admin_keyboard())
    else:
        await message.answer("Возврат в главное меню.", reply_markup=get_main_keyboard())

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("❌ Нет активного действия для отмены.")
    else:
        await state.clear()
        await message.answer("✅ Действие отменено.")
    await message.answer("Главное меню:", reply_markup=get_main_keyboard())

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id == bot.id:
        await message.answer("⛔ Бот не может использовать команды для себя.")
        return
    user = await get_user(user_id)
    if not user:
        await create_user(
            user_id,
            message.from_user.username,
            message.from_user.first_name,
            message.from_user.last_name
        )
    args = message.text.split()
    if len(args) > 1 and args[1].startswith('gift_'):
        code = args[1][5:]
        cert = await get_gift_certificate_by_code(code)
        if cert and cert['status'] == 'active':
            price = await get_price()
            amount = cert['amount']
            if amount >= price:
                final_price_text = "✅ Сертификат полностью покрывает стоимость разбора!"
            else:
                final_price_text = f"Цена после применения сертификата: {price - amount} ₽."
            await message.answer(
                f"🎁 Вы активировали подарочный сертификат на {amount} ₽ \n\n"
                f"Вы можете использовать этот сертификат для заказа персонального разбора вашей карты Ба Цзы.\n\n"
                f"Код сертификата:\n"
                f"<code>{code}</code>\n\n"
                f"Что такое Ба Цзы?\n"
                f"Ба Цзы - это древняя китайская метафизическая система, которая по дате, времени и месту вашего рождения строит вашу уникальную карту личности и судьбы. Она анализирует взаимодействие Небесных Стволов и Земных Ветвей — десяти энергетических столпов, формирующих вашу личность, судьбу и жизненные циклы.\n\n"
                f"На основе этого анализа вы узнаете:\n\n"
                f"📜 Хозяина Дня — ваш главный элемент\n"
                f"📜 Архетип личности и особенности характера\n"
                f"📜 Баланс 5 элементов в вашей карте\n"
                f"📜 Сильные и слабые стороны, ваш потенциал\n"
                f"📜 Уязвимости и защитные механизмы\n"
                f"📜 Анализ десятилетних тактов (столпов удачи) — периоды взлётов и спадов\n"
                f"📜 Советы по гармонизации элементов\n"
                f"📜 Прогноз на ближайшие 10 лет\n\n"
                f"Вы получите красиво стилизованный PDF-документ, который можно сохранить и перечитывать в любое время.\n\n"
                f"💰 Стоимость разбора: {price} ₽\n\n"
                f"✅ Введите код вашего сертификата:  <code>{code}</code>  при оформлении заказа (нажмите на код чтобы скопировать).\n\n"
                f"{final_price_text}\n\n"
                f"Воспользуйтесь сертификатом — нажмите «Заказать разбор»!",
                reply_markup=get_main_keyboard(),
                parse_mode="HTML"
            )
        else:
            await message.answer("❌ Сертификат не найден или уже использован.")
            if user_id in ADMIN_IDS:
                await message.answer("Панель администратора:", reply_markup=get_admin_keyboard())
            else:
                price = await get_price()
                await message.answer(
                    f"🌟 Добро пожаловать в BaziExpertBot!\n\n"
                    f"💰 Стоимость разбора: {price} ₽",
                    reply_markup=get_main_keyboard()
                )
        return

    if user_id in ADMIN_IDS:
        await message.answer(
            "👋 Добро пожаловать в панель администратора!",
            reply_markup=get_admin_keyboard()
        )
        return
    price = await get_price()
    text = (
        "🌟 Здравствуйте! Добро пожаловать в BaziExpert!\n\n"
        "Что такое Ба Цзы?\n"
        "Ба Цзы (Четыре Столпа Судьбы) — это древняя китайская метафизическая система, которая по дате, времени и месту вашего рождения строит вашу уникальную карту личности и судьбы. Она анализирует взаимодействие Небесных Стволов и Земных Ветвей — десяти энергетических столпов, формирующих вашу личность, судьбу и жизненные циклы.\n\n"
        "Наш специалист построит вашу персональную карту Ба Цзы и сделает для вас подробный многостраничный разбор, в котором вы узнаете:\n\n"
        "🧘‍♂️ Хозяина Дня — ваш главный элемент личности\n"
        "⚖️ Баланс 5 элементов в вашей карте\n"
        "💪 Сильные и слабые стороны, ваш потенциал и зоны роста\n"
        "🛡️ Уязвимости и защитные механизмы\n"
        "🎭 Архетип личности и особенности характера\n"
        "📈 Анализ десятилетних тактов (столпов удачи) — периоды взлётов и спадов\n"
        "🔮 Прогноз удачных и неудачных периодов на ближайшие 10 лет\n"
        "🌿 Практические советы, как сбалансировать элементы и улучшить качество жизни\n\n"
        "Всё это вы получите в красиво стилизованном PDF-документе, который можно сохранить и перечитывать в любое время.\n\n"
        f"💰 Стоимость разбора: {price} ₽\n\n"
        "Готовы узнать свою судьбу? Нажмите кнопку ниже, чтобы заказать разбор!"
    )
    await message.answer(text, reply_markup=get_main_keyboard())

@dp.message(F.text == "📋 Мои заказы")
async def cmd_my_orders(message: types.Message):
    user_id = message.from_user.id
    if user_id == bot.id:
        await message.answer("⛔ Бот не может просматривать заказы для себя.")
        return
    orders = await get_orders_by_user(user_id)
    if not orders:
        await message.answer("📭 У вас пока нет заказов.")
        return

    text = "📋 **Ваши заказы:**\n\n"
    for order in orders[:10]:
        status_emoji = {
            'pending': '🆕',
            'pending_payment': '⏳',
            'paid': '✅',
            'processing': '🔨',
            'done': '📄',
            'cancelled': '🚫'
        }.get(order['status'], '❓')
        status_desc = {
            'pending': 'Ожидает оплаты',
            'pending_payment': 'Ожидает оплаты',
            'paid': 'Оплачен, ожидает начала',
            'processing': 'В процессе подготовки',
            'done': 'Готов',
            'cancelled': 'Отменён'
        }.get(order['status'], 'Неизвестный статус')
        local_created = (order['created_at'] + timedelta(hours=3)).strftime('%d.%m.%Y %H:%M') + " (МСК)" if order['created_at'] else '—'
        text += f"{status_emoji} Заказ №{order['id']} – {status_desc}\n"
        text += f"   👤 Ваше имя: {order['client_name'] or 'Не указано'}\n"
        if order['birth_date']:
            text += f"   📅 {local_created} | 💰 {order['total_price']} ₽\n\n"
        else:
            text += f"   📅 {local_created} | 💰 {order['total_price']} ₽ (данные удалены)\n\n"

    text += "Выберите заказ для управления:"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for order in orders[:10]:
        if order['status'] in ('pending', 'pending_payment'):
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(
                    text=f"💳 Оплатить №{order['id']}",
                    callback_data=f"pay_order_{order['id']}"
                ),
                InlineKeyboardButton(
                    text=f"❌ Отменить №{order['id']}",
                    callback_data=f"cancel_order_{order['id']}"
                )
            ])
        elif order['status'] in ('paid', 'processing'):
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(
                    text=f"📊 Статус №{order['id']}",
                    callback_data=f"status_order_{order['id']}"
                )
            ])
        elif order['status'] == 'done' and order['file_id']:
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(
                    text=f"📄 Скачать №{order['id']}",
                    callback_data=f"download_order_{order['id']}"
                )
            ])
    if not keyboard.inline_keyboard:
        keyboard.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")])
    else:
        keyboard.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")])
    await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)

@dp.callback_query(F.data.startswith("pay_order_"))
async def pay_order_callback(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if user_id == bot.id:
        await callback.answer("⛔ Бот не может оплачивать заказы.", show_alert=True)
        return
    order_id = int(callback.data.split("_")[2])
    order = await get_order(order_id)
    if not order:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    if order['status'] not in ('pending', 'pending_payment'):
        await callback.answer("Этот заказ уже оплачен или не требует оплаты", show_alert=True)
        return
    if order['user_id'] != user_id:
        await callback.answer("Это не ваш заказ", show_alert=True)
        return
    price = order['total_price']
    if price <= 0:
        await callback.answer("Сумма заказа равна 0", show_alert=True)
        return
    
    if user_id in ADMIN_IDS:
        await state.update_data(order_id=order_id, final_price=price, order_data=dict(order))
        await callback.message.edit_text(
            "Выберите режим оплаты:",
            reply_markup=get_payment_choice_keyboard()
        )
        await state.set_state(OrderStates.waiting_payment_choice)
        await callback.answer()
        return
    
    payload = f"bazi_{order_id}_{int(datetime.now().timestamp())}"
    try:
        await bot.send_invoice(
            chat_id=user_id,
            title="Разбор карты Ба Цзы",
            description=(
                f"Заказ №{order_id}\n"
                f"Персональный разбор вашей карты Ба Цзы\n"
                f"Дата рождения: {order['birth_date']}\n"
                f"Время: {order['birth_time']}\n"
                f"Место рождения: {order['birth_city']}"
            ),
            payload=payload,
            provider_token=get_provider_token(user_id),
            currency="RUB",
            prices=[LabeledPrice(label="Разбор Ба Цзы", amount=int(price * 100))],
            start_parameter="bazi_order",
            need_email=True,
            need_phone_number=False,
        )
        await update_order_status(order_id, 'pending_payment')
        await callback.message.edit_text(f"💳 Счёт для заказа №{order_id} отправлен. Оплатите его, чтобы завершить оформление.")
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка повторной отправки инвойса: {e}")
        await callback.answer(f"❌ Ошибка: {e}", show_alert=True)

@dp.callback_query(F.data.startswith("cancel_order_"))
async def cancel_order_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id == bot.id:
        await callback.answer("⛔ Бот не может отменять заказы.", show_alert=True)
        return
    order_id = int(callback.data.split("_")[2])
    order = await get_order(order_id)
    if not order:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    if order['user_id'] != user_id:
        await callback.answer("Это не ваш заказ", show_alert=True)
        return
    if order['status'] in ('done', 'cancelled'):
        await callback.answer("Этот заказ уже завершён или отменён", show_alert=True)
        return

    if order['gift_code'] and order['gift_amount_used'] > 0:
        await restore_gift_certificate(order['gift_code'], order['gift_amount_used'])

    await clear_order_gift(order_id)
    await update_order_status(order_id, 'cancelled')
    await callback.message.edit_text(f"✅ Заказ №{order_id} отменён. Инвойс стал недействительным, не оплачивайте его.")
    await callback.answer("Заказ отменён")
    await callback.message.answer("Главное меню:", reply_markup=get_main_keyboard())

@dp.callback_query(F.data.startswith("status_order_"))
async def status_order_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id == bot.id:
        await callback.answer("⛔ Бот не может проверять статус.", show_alert=True)
        return
    order_id = int(callback.data.split("_")[2])
    order = await get_order(order_id)
    if not order:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    if order['user_id'] != user_id:
        await callback.answer("Это не ваш заказ", show_alert=True)
        return
    status_desc = {
        'pending': 'Ожидает оплаты',
        'pending_payment': 'Ожидает оплаты',
        'paid': 'Оплачен, ожидает начала',
        'processing': 'В процессе подготовки',
        'done': 'Готов',
        'cancelled': 'Отменён'
    }.get(order['status'], 'Неизвестный статус')
    await callback.message.answer(f"📊 Статус заказа №{order_id}: {status_desc}")
    await callback.answer()

@dp.callback_query(F.data.startswith("download_order_"))
async def download_order_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id == bot.id:
        await callback.answer("⛔ Бот не может скачивать файлы.", show_alert=True)
        return
    order_id = int(callback.data.split("_")[2])
    order = await get_order(order_id)
    if not order:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    if order['user_id'] != user_id:
        await callback.answer("Это не ваш заказ", show_alert=True)
        return
    if not order['file_id']:
        await callback.answer("Файл отсутствует", show_alert=True)
        return
    try:
        await bot.send_document(
            user_id,
            order['file_id'],
            caption=f"📄 Ваш разбор по заказу №{order_id}"
        )
        await callback.answer("Файл отправлен")
    except Exception as e:
        logger.error(f"Ошибка отправки файла: {e}")
        await callback.answer("Ошибка при отправке файла", show_alert=True)

@dp.callback_query(F.data == "main_menu")
async def main_menu_callback(callback: types.CallbackQuery):
    await callback.message.answer("Главное меню:", reply_markup=get_main_keyboard())
    await callback.answer()

# ============================================================
# ОБРАБОТЧИК "ЗАКАЗАТЬ РАЗБОР" с проверкой статуса платежей
# ============================================================
@dp.message(F.text == "📝 Заказать разбор")
async def cmd_order(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id == bot.id:
        await message.answer("⛔ Бот не может оформлять заказы для себя.")
        return
    
    if not await is_payments_enabled() and user_id not in ADMIN_IDS:
        await message.answer(
            "⛔ В данный момент приём платежей временно приостановлен. Пожалуйста, попробуйте позже."
        )
        return

    await ensure_user_exists(
        user_id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name
    )

    unpaid_orders = await get_orders_by_status('pending_payment')
    unpaid = [o for o in unpaid_orders if o['user_id'] == user_id and o['status'] in ('pending', 'pending_payment')]
    if unpaid:
        text = "У вас есть неоплаченный(е) заказ(ы). Выберите действие:\n"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[])
        for order in unpaid[:5]:
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(
                    text=f"💳 Оплатить заказ №{order['id']}",
                    callback_data=f"pay_order_{order['id']}"
                ),
                InlineKeyboardButton(
                    text=f"❌ Отменить заказ №{order['id']}",
                    callback_data=f"cancel_order_{order['id']}"
                )
            ])
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(
                text="✅ Создать новый заказ (старые неоплаченные будут отменены)",
                callback_data="create_new_order_force"
            )
        ])
        keyboard.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")])
        await message.answer(text, reply_markup=keyboard)
        return

    text = (
        "📅 Для составления персональной карты Ба Цзы нам необходимы ваши данные: дата, время и место рождения.\n\n"
        "Указывая свои данные, вы соглашаетесь с условиями:\n"
        "📄 <a href=\"https://telegra.ph/PUBLICHNAYA-OFERTA-POLZOVATELSKOE-SOGLASHENIE-07-14\">Пользовательское соглашение</a>\n"
        "🔒 <a href=\"https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-07-14-83\">Политика конфиденциальности</a>\n\n"
        "Введите дату рождения в формате ДД.ММ.ГГГГ\n"
        "Например: 15.08.1990"
    )
    await message.answer(text, reply_markup=get_home_keyboard(), parse_mode="HTML", disable_web_page_preview=True)
    await state.set_state(OrderStates.waiting_birth_date)

@dp.callback_query(F.data == "create_new_order_force")
async def create_new_order_force(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if user_id == bot.id:
        await callback.answer("⛔ Бот не может создавать заказы.", show_alert=True)
        return
    await ensure_user_exists(
        user_id,
        callback.from_user.username,
        callback.from_user.first_name,
        callback.from_user.last_name
    )

    orders = await get_orders_by_user(user_id)
    for o in orders:
        if o['status'] in ('pending', 'pending_payment'):
            if o['gift_code'] and o['gift_amount_used'] > 0:
                await restore_gift_certificate(o['gift_code'], o['gift_amount_used'])
            await clear_order_gift(o['id'])
            await update_order_status(o['id'], 'cancelled')
    await state.clear()
    await callback.message.edit_text("✅ Все старые неоплаченные заказы отменены. Начинаем оформление нового заказа.")
    text = (
        "📅 Для составления персональной карты Ба Цзы нам необходимы ваши данные: дата, время и место рождения.\n\n"
        "Указывая свои данные, вы соглашаетесь с условиями:\n"
        "📄 <a href=\"https://telegra.ph/PUBLICHNAYA-OFERTA-POLZOVATELSKOE-SOGLASHENIE-07-14\">Пользовательское соглашение</a>\n"
        "🔒 <a href=\"https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-07-14-83\">Политика конфиденциальности</a>\n\n"
        "Введите дату рождения в формате ДД.ММ.ГГГГ\n"
        "Например: 15.08.1990"
    )
    await callback.message.answer(text, reply_markup=get_home_keyboard(), parse_mode="HTML", disable_web_page_preview=True)
    await state.set_state(OrderStates.waiting_birth_date)
    await callback.answer()

@dp.callback_query(F.data == "create_new_order")
async def create_new_order_callback(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if user_id == bot.id:
        await callback.answer("⛔ Бот не может создавать заказы.", show_alert=True)
        return
    await ensure_user_exists(
        user_id,
        callback.from_user.username,
        callback.from_user.first_name,
        callback.from_user.last_name
    )
    await state.clear()
    if callback.message:
        await callback.message.edit_text(
            "✅ Начинаем оформление нового заказа. "
            "Ваши предыдущие заказы останутся активными."
        )
    text = (
        "📅 Для составления персональной карты Ба Цзы нам необходимы ваши данные: дата, время и место рождения.\n\n"
        "Указывая свои данные, вы соглашаетесь с условиями:\n"
        "📄 <a href=\"https://telegra.ph/PUBLICHNAYA-OFERTA-POLZOVATELSKOE-SOGLASHENIE-07-14\">Пользовательское соглашение</a>\n"
        "🔒 <a href=\"https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-07-14-83\">Политика конфиденциальности</a>\n\n"
        "Введите дату рождения в формате ДД.ММ.ГГГГ\n"
        "Например: 15.08.1990"
    )
    await callback.message.answer(text, reply_markup=get_home_keyboard(), parse_mode="HTML", disable_web_page_preview=True)
    await state.set_state(OrderStates.waiting_birth_date)
    await callback.answer()

@dp.message(OrderStates.waiting_birth_date)
async def process_birth_date(message: types.Message, state: FSMContext):
    birth_date = message.text.strip()
    try:
        datetime.strptime(birth_date, "%d.%m.%Y")
    except ValueError:
        await message.answer("❌ Неверный формат! Введите ДД.ММ.ГГГГ", reply_markup=get_home_keyboard())
        return
    await state.update_data(birth_date=birth_date)
    await message.answer(
        "🕐 Введите время рождения в формате ЧЧ:ММ (например, 14:30).",
        reply_markup=get_back_home_keyboard()
    )
    await state.set_state(OrderStates.waiting_birth_time)

@dp.message(OrderStates.waiting_birth_time)
async def process_birth_time(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.set_state(OrderStates.waiting_birth_date)
        await message.answer(
            "📅 Введите дату рождения в формате ДД.ММ.ГГГГ\nНапример: 15.08.1990",
            reply_markup=get_home_keyboard()
        )
        return
    birth_time = message.text.strip()
    try:
        datetime.strptime(birth_time, "%H:%M")
    except ValueError:
        await message.answer("❌ Неверный формат! Введите ЧЧ:ММ", reply_markup=get_back_home_keyboard())
        return
    await state.update_data(birth_time=birth_time)
    await message.answer(
        "🏙️ Введите место рождения:",
        reply_markup=get_back_home_keyboard()
    )
    await state.set_state(OrderStates.waiting_city)

@dp.message(OrderStates.waiting_city)
async def process_city(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.set_state(OrderStates.waiting_birth_time)
        await message.answer(
            "🕐 Введите время рождения в формате ЧЧ:ММ (например, 14:30).",
            reply_markup=get_back_home_keyboard()
        )
        return
    city = message.text.strip()
    await state.update_data(birth_city=city)
    await message.answer(
        "👤 Укажите ваш пол:",
        reply_markup=get_gender_keyboard()
    )
    await state.set_state(OrderStates.waiting_gender)

@dp.message(OrderStates.waiting_gender)
async def process_gender(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.set_state(OrderStates.waiting_city)
        await message.answer(
            "🏙️ Введите место рождения:",
            reply_markup=get_back_home_keyboard()
        )
        return
    if "Мужской" in message.text:
        gender = "male"
    elif "Женский" in message.text:
        gender = "female"
    else:
        await message.answer("Пожалуйста, выберите пол, нажав кнопку.", reply_markup=get_gender_keyboard())
        return
    await state.update_data(gender=gender)
    await message.answer(
        "👤 Введите ваше имя (которое будет в разборе):",
        reply_markup=get_back_home_keyboard()
    )
    await state.set_state(OrderStates.waiting_name)

@dp.message(OrderStates.waiting_name)
async def process_name(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.set_state(OrderStates.waiting_gender)
        await message.answer(
            "👤 Укажите ваш пол:",
            reply_markup=get_gender_keyboard()
        )
        return
    name = message.text.strip()
    if not name:
        await message.answer("❌ Имя не может быть пустым. Пожалуйста, введите ваше имя:", reply_markup=get_back_home_keyboard())
        return
    await state.update_data(client_name=name)
    await message.answer(
        "📧 Введите email (необязательно) или нажмите «Пропустить»:",
        reply_markup=get_email_keyboard()
    )
    await state.set_state(OrderStates.waiting_email)

@dp.message(OrderStates.waiting_email)
async def process_email(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await state.set_state(OrderStates.waiting_name)
        await message.answer(
            "👤 Введите ваше имя (которое будет в разборе):",
            reply_markup=get_back_home_keyboard()
        )
        return
    email = message.text.strip()
    if email == "⏭️ Пропустить":
        email = None
    else:
        if '@' not in email or '.' not in email:
            await message.answer("❌ Введите корректный email или нажмите «Пропустить»", reply_markup=get_email_keyboard())
            return
    await state.update_data(email=email)
    await show_summary(message, state)

async def show_summary(message: types.Message, state: FSMContext):
    data = await state.get_data()
    text = (
        "📋 Проверьте введённые данные:\n\n"
        f"👤 Имя: {data.get('client_name', '—')}\n"
        f"📅 Дата рождения: {data['birth_date']}\n"
        f"🕐 Время: {data['birth_time']}\n"
        f"🏙️ Место рождения: {data['birth_city']}\n"
        f"👤 Пол: {'Мужской' if data['gender'] == 'male' else 'Женский'}\n"
        f"📧 Email: {data.get('email') or 'Не указан'}\n"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_data")],
            [InlineKeyboardButton(text="✏️ Изменить данные", callback_data="edit_data")]
        ]
    )
    await message.answer(text, reply_markup=keyboard)
    await state.set_state(OrderStates.confirm_summary)

async def show_edit_field_choice(target, state):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Дата рождения", callback_data="edit_field_birth_date")],
            [InlineKeyboardButton(text="🕐 Время рождения", callback_data="edit_field_birth_time")],
            [InlineKeyboardButton(text="🏙️ Место рождения", callback_data="edit_field_birth_city")],
            [InlineKeyboardButton(text="👤 Пол", callback_data="edit_field_gender")],
            [InlineKeyboardButton(text="👤 Имя", callback_data="edit_field_client_name")],
            [InlineKeyboardButton(text="📧 Email", callback_data="edit_field_email")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_edit")]
        ]
    )
    if hasattr(target, 'edit_text'):
        await target.edit_text("✏️ Что вы хотите изменить?", reply_markup=keyboard)
    else:
        await target.answer("✏️ Что вы хотите изменить?", reply_markup=keyboard)
    await state.set_state(OrderStates.editing_field)

@dp.callback_query(F.data == "edit_data")
async def edit_data_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await show_edit_field_choice(callback.message, state)

@dp.callback_query(F.data.startswith("edit_field_"))
async def edit_field_selected(callback: types.CallbackQuery, state: FSMContext):
    field = callback.data.replace("edit_field_", "")
    await state.update_data(editing_field=field)
    field_names = {
        'birth_date': 'дату рождения (ДД.ММ.ГГГГ)',
        'birth_time': 'время рождения (ЧЧ:ММ)',
        'birth_city': 'место рождения',
        'gender': 'пол (Мужской/Женский)',
        'client_name': 'имя',
        'email': 'email (или напишите "пропустить")'
    }
    await callback.message.edit_text(f"Вы выбрали поле «{field_names.get(field, field)}». Введите новое значение в следующем сообщении.")
    await callback.message.answer(
        f"Введите новое значение для поля «{field_names.get(field, field)}»:",
        reply_markup=get_back_home_keyboard()
    )
    await state.set_state(OrderStates.editing_value)
    await callback.answer()

@dp.message(StateFilter(OrderStates.editing_value))
async def process_edit_value(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await show_edit_field_choice(message, state)
        return
    data = await state.get_data()
    field = data.get('editing_field')
    if not field:
        await message.answer("❌ Произошла ошибка. Попробуйте заново.", reply_markup=get_back_home_keyboard())
        await state.clear()
        return

    value = message.text.strip()
    if field == 'birth_date':
        try:
            datetime.strptime(value, "%d.%m.%Y")
        except ValueError:
            await message.answer("❌ Неверный формат! Введите ДД.ММ.ГГГГ", reply_markup=get_back_home_keyboard())
            return
    elif field == 'birth_time':
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError:
            await message.answer("❌ Неверный формат! Введите ЧЧ:ММ", reply_markup=get_back_home_keyboard())
            return
    elif field == 'gender':
        if value not in ['male', 'female']:
            if "муж" in value.lower():
                value = "male"
            elif "жен" in value.lower():
                value = "female"
            else:
                await message.answer("❌ Введите «Мужской» или «Женский»", reply_markup=get_back_home_keyboard())
                return
    elif field == 'email':
        if value.lower() == 'пропустить':
            value = None
        elif '@' not in value or '.' not in value:
            await message.answer("❌ Введите корректный email или напишите «пропустить»", reply_markup=get_back_home_keyboard())
            return

    await state.update_data({field: value})
    await show_summary(message, state)
    await state.update_data(editing_field=None)

@dp.callback_query(F.data == "cancel_edit")
async def cancel_edit(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await show_summary(callback.message, state)

@dp.callback_query(F.data == "confirm_data")
async def confirm_data(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "✅ Данные подтверждены. Теперь укажите подарочный сертификат, если есть.",
        reply_markup=None
    )
    await callback.message.answer(
        "Если у вас есть подарочный сертификат, нажмите на кнопку и введите код; если нет – нажмите «Пропустить».",
        reply_markup=get_gift_code_choice_keyboard()
    )
    await state.set_state(OrderStates.waiting_gift_code_input)
    await callback.answer()

@dp.callback_query(F.data == "enter_gift_code")
async def enter_gift_code(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "📝 Введите код подарочного сертификата (например, GIFT-ABC123):",
        reply_markup=None
    )
    await state.set_state(OrderStates.waiting_gift_code_input)
    await callback.answer()

@dp.callback_query(F.data == "skip_gift_code")
async def skip_gift_code(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "⏭️ Вы пропустили ввод кода сертификата.",
        reply_markup=None
    )
    await state.update_data(gift_code=None, gift_balance=0)
    await confirm_order_final(callback.message, state, user_id=callback.from_user.id)
    await callback.answer()

@dp.message(OrderStates.waiting_gift_code_input)
async def process_gift_code_input(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id == bot.id:
        await message.answer("⛔ Бот не может использовать сертификаты.")
        return
    text = message.text.strip()
    if text == "⏭️ Пропустить" or text == "пропустить":
        await state.update_data(gift_code=None, gift_balance=0)
        await confirm_order_final(message, state)
        return
    cert = await get_gift_certificate_by_code(text)
    if not cert or cert['status'] != 'active':
        await message.answer(
            "❌ Неверный код или сертификат уже использован. Попробуйте снова или нажмите «Пропустить».",
            reply_markup=get_gift_code_choice_keyboard()
        )
        return
    pending_orders = await get_pending_orders_with_gift_code(user_id, text)
    if pending_orders:
        order_nums = ', '.join([str(o['id']) for o in pending_orders])
        await message.answer(
            f"❌ Этот сертификат уже используется в вашем заказе(ах) №{order_nums}.\n"
            f"Дождитесь его оплаты или отмените его, чтобы использовать сертификат в новом заказе.",
            reply_markup=get_gift_code_choice_keyboard()
        )
        return
    await state.update_data(gift_code=text, gift_balance=cert['amount'], gift_cert=cert)
    await confirm_order_final(message, state)

# Функция для отправки инвойса с выбором токена
async def send_invoice_with_token(user_id: int, order_id: int, final_price: int, data: dict, token_type: str = "live"):
    token = get_provider_token(user_id, token_type)
    payload = f"bazi_{order_id}_{int(datetime.now().timestamp())}"
    try:
        await bot.send_invoice(
            chat_id=user_id,
            title="Разбор карты Ба Цзы",
            description=(
                f"Заказ №{order_id}\n"
                f"Персональный разбор вашей карты Ба Цзы\n"
                f"Дата рождения: {data['birth_date']}\n"
                f"Время: {data['birth_time']}\n"
                f"Место рождения: {data['birth_city']}"
            ),
            payload=payload,
            provider_token=token,
            currency="RUB",
            prices=[LabeledPrice(label="Разбор Ба Цзы", amount=int(final_price * 100))],
            start_parameter="bazi_order",
            need_email=True,
            need_phone_number=False,
        )
        await update_order_status(order_id, 'pending_payment')
        return True
    except Exception as e:
        logger.error(f"Ошибка отправки инвойса: {e}")
        raise e

async def confirm_order_final(message: types.Message, state: FSMContext, user_id: int = None):
    try:
        if user_id is None:
            user_id = message.from_user.id

        if user_id == bot.id:
            await message.answer("⛔ Бот не может оформлять заказы для себя.")
            await state.clear()
            return

        if not await ensure_user_exists(
            user_id,
            message.from_user.username,
            message.from_user.first_name,
            message.from_user.last_name
        ):
            await message.answer("❌ Не удалось создать профиль пользователя. Попробуйте позже.")
            await state.clear()
            return

        data = await state.get_data()
        price = await get_price()
        gift_code = data.get('gift_code')
        gift_balance = data.get('gift_balance', 0)
        final_price = price
        gift_amount_used = 0

        if gift_code and gift_balance > 0:
            if gift_balance >= price:
                final_price = 0
                gift_amount_used = price
                await message.answer(f"✅ Сертификат {gift_code} полностью покрывает стоимость заказа. Оплата не требуется.")
            else:
                final_price = price - gift_balance
                gift_amount_used = gift_balance
                await message.answer(f"✅ Применён сертификат {gift_code} на {gift_balance} ₽. Осталось оплатить {final_price} ₽.")

        order_id = await create_order(
            user_id=user_id,
            birth_date=data['birth_date'],
            birth_time=data['birth_time'],
            birth_city=data['birth_city'],
            gender=data['gender'],
            email=data.get('email'),
            client_name=data.get('client_name', 'Клиент'),
            total_price=final_price,
            gift_code=gift_code,
            gift_amount_used=gift_amount_used
        )

        if final_price == 0:
            await update_order_status(order_id, 'paid')
            if gift_code:
                await update_gift_balance_and_status(gift_code, gift_amount_used, order_id, user_id)
            user = await get_user(user_id)
            await notify_admin_new_order(order_id, user)
            await message.answer(
                f"✅ Ваш заказ №{order_id} оформлен и оплачен сертификатом!\n"
                "Специалист приступит к работе в ближайшее время, вы получите уведомление о начале работы с вашим заказом.",
                reply_markup=get_main_keyboard()
            )
            await message.answer(
                "📌 Вы можете заказать новый разбор или проверить статус существующих заказов.",
                reply_markup=get_status_button_keyboard()
            )
            await state.clear()
        else:
            if user_id in ADMIN_IDS:
                await state.update_data(order_id=order_id, final_price=final_price, order_data=data)
                await message.answer(
                    "Выберите режим оплаты:",
                    reply_markup=get_payment_choice_keyboard()
                )
                await state.set_state(OrderStates.waiting_payment_choice)
                return

            await send_invoice_with_token(user_id, order_id, final_price, data, "live")
            await state.clear()
    except Exception as e:
        logger.error(f"Ошибка в confirm_order_final: {e}")
        await message.answer("❌ Произошла ошибка при оформлении заказа. Пожалуйста, попробуйте позже.")
        await state.clear()

# Обработчик выбора режима платежа для админов
@dp.callback_query(F.data.startswith("pay_choice_"))
async def payment_choice_callback(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if user_id not in ADMIN_IDS:
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return

    choice = callback.data.split("_")[2]
    if choice == "cancel":
        await callback.message.edit_text("❌ Оплата отменена.")
        await state.clear()
        await callback.answer()
        return

    data = await state.get_data()
    logger.info(f"Payment choice data: {data}")

    # Проверяем наличие данных для заказа
    if 'order_id' in data and 'final_price' in data and 'order_data' in data:
        order_id = data['order_id']
        final_price = data['final_price']
        order_data = data['order_data']
        try:
            await send_invoice_with_token(user_id, order_id, final_price, order_data, choice)
            await callback.message.edit_text(f"💳 Счёт для заказа №{order_id} отправлен. Оплатите его, чтобы завершить оформление.")
            await state.clear()
            await callback.answer()
        except Exception as e:
            logger.error(f"Ошибка отправки инвойса для заказа: {e}")
            await callback.message.edit_text(f"❌ Не удалось создать платёжный счёт. Ошибка: {e}")
            await state.clear()
            await callback.answer()
        return

    # Проверяем наличие данных для сертификата
    elif 'gift_amount' in data:
        gift_amount = data['gift_amount']
        gift_message = data.get('gift_message')
        # Создаём временную запись в БД перед отправкой инвойса
        pending_id = await create_pending_gift_certificate(user_id, gift_amount, gift_message)
        token = get_provider_token(user_id, choice)
        payload = f"gift_{pending_id}_{user_id}"
        try:
            await bot.send_invoice(
                chat_id=user_id,
                title="Подарочный сертификат",
                description=f"Сертификат на сумму {gift_amount} ₽ для оплаты услуг BaziExpert.",
                payload=payload,
                provider_token=token,
                currency="RUB",
                prices=[LabeledPrice(label=f"Сертификат {gift_amount} ₽", amount=int(gift_amount * 100))],
                start_parameter="gift_purchase",
                need_email=False,
                need_phone_number=False,
            )
            await callback.message.edit_text("💳 Оплатите счёт для получения сертификата.")
            # Очищаем состояние, но данные уже сохранены в БД
            await state.clear()
            await callback.answer()
        except Exception as e:
            logger.error(f"Ошибка отправки инвойса на сертификат: {e}")
            await callback.message.edit_text(f"❌ Ошибка: {e}")
            await state.clear()
            await callback.answer()
        return

    else:
        await callback.message.edit_text("❌ Ошибка: не удалось определить тип оплаты. Попробуйте заново.")
        await state.clear()
        await callback.answer()
        return

@dp.message(F.text == "🎁 Подарочный сертификат")
async def cmd_gift_certificate(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if user_id == bot.id:
        await message.answer("⛔ Бот не может покупать сертификаты.")
        return
    
    if not await is_payments_enabled() and user_id not in ADMIN_IDS:
        await message.answer("⛔ В данный момент приём платежей временно приостановлен. Пожалуйста, попробуйте позже.")
        return
    
    await message.answer(
        "Выберите номинал подарочного сертификата:",
        reply_markup=get_gift_amount_keyboard()
    )
    await state.set_state(OrderStates.waiting_gift_amount)

@dp.callback_query(F.data.startswith("gift_"))
async def gift_amount_selected(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if user_id == bot.id:
        await callback.answer("⛔ Бот не может покупать сертификаты.", show_alert=True)
        return
    amount = int(callback.data.split('_')[1])
    await state.update_data(gift_amount=amount)
    await callback.message.edit_text(
        f"Вы выбрали сертификат на {amount} ₽.\n\n"
        "Хотите добавить поздравительный текст для получателя?",
        reply_markup=get_gift_message_choice_keyboard()
    )
    await state.set_state(OrderStates.waiting_gift_message_choice)
    await callback.answer()

@dp.callback_query(F.data == "skip_gift_message")
async def skip_gift_message(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(gift_message=None)
    await callback.message.edit_text("⏭️ Вы пропустили добавление поздравительного текста.")
    await show_gift_confirm(callback.message, state)
    await callback.answer()

@dp.callback_query(F.data == "write_gift_message")
async def write_gift_message(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "✏️ Введите поздравительный текст (максимум 300 символов):\n"
        "Например: «Дорогая Мария! С днём рождения! Желаю счастья и удачи!»"
    )
    await state.set_state(OrderStates.waiting_gift_message_text)
    await callback.answer()

@dp.message(StateFilter(OrderStates.waiting_gift_message_text))
async def process_gift_message_text(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if len(text) > 300:
        await message.answer("❌ Текст слишком длинный. Максимум 300 символов. Пожалуйста, сократите.", reply_markup=get_back_home_keyboard())
        return
    await state.update_data(gift_message=text)
    await message.answer(
        f"📝 Ваш поздравительный текст:\n\n"
        f"«{text}»\n\n"
        "Подтвердите или измените:",
        reply_markup=get_gift_message_confirm_keyboard()
    )
    await state.set_state(OrderStates.waiting_gift_message_confirm)

@dp.callback_query(F.data == "edit_gift_message")
async def edit_gift_message(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "✏️ Введите новый поздравительный текст (максимум 300 символов):"
    )
    await state.set_state(OrderStates.waiting_gift_message_text)
    await callback.answer()

@dp.callback_query(F.data == "confirm_gift_message")
async def confirm_gift_message(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("✅ Поздравительный текст сохранён.")
    await show_gift_confirm(callback.message, state)
    await callback.answer()

async def show_gift_confirm(message: types.Message, state: FSMContext):
    data = await state.get_data()
    amount = data.get('gift_amount')
    gift_message = data.get('gift_message')
    text = f"Вы выбрали сертификат на {amount} ₽.\n"
    if gift_message:
        text += f"\n💌 Поздравление: <blockquote>{gift_message}</blockquote>\n"
    text += "\nПодтвердите покупку."
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить покупку", callback_data="confirm_gift_purchase")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_gift")]
        ]
    )
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await state.set_state(OrderStates.waiting_gift_confirm)

@dp.callback_query(F.data == "confirm_gift_purchase")
async def confirm_gift_purchase(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if user_id == bot.id:
        await callback.answer("⛔ Бот не может покупать сертификаты.", show_alert=True)
        return
    data = await state.get_data()
    amount = data.get('gift_amount')
    gift_message = data.get('gift_message')
    if not amount:
        await callback.answer("Ошибка: выберите номинал.", show_alert=True)
        return
    
    if user_id in ADMIN_IDS:
        # Для админа сохраняем данные в состояние для последующего выбора токена
        await state.update_data(gift_amount=amount, gift_message=gift_message)
        await callback.message.edit_text(
            "Выберите режим оплаты для сертификата:",
            reply_markup=get_payment_choice_keyboard()
        )
        await state.set_state(OrderStates.waiting_payment_choice)
        await callback.answer()
        return
    
    # Для обычных пользователей – создаём запись в БД и отправляем инвойс
    pending_id = await create_pending_gift_certificate(user_id, amount, gift_message)
    payload = f"gift_{pending_id}_{user_id}"
    await state.update_data(payload=payload, gift_message=gift_message)  # на всякий случай
    try:
        await bot.send_invoice(
            chat_id=user_id,
            title="Подарочный сертификат",
            description=f"Сертификат на сумму {amount} ₽ для оплаты услуг BaziExpert.",
            payload=payload,
            provider_token=get_provider_token(user_id),
            currency="RUB",
            prices=[LabeledPrice(label=f"Сертификат {amount} ₽", amount=int(amount * 100))],
            start_parameter="gift_purchase",
            need_email=False,
            need_phone_number=False,
        )
        await callback.message.edit_text("💳 Оплатите счёт для получения сертификата.")
        await state.clear()
    except Exception as e:
        logger.error(f"Ошибка отправки инвойса на сертификат: {e}")
        await callback.message.edit_text(f"❌ Ошибка: {e}")
    await callback.answer()

@dp.callback_query(F.data == "cancel_gift")
async def cancel_gift(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("❌ Покупка сертификата отменена.")
    await state.clear()
    await callback.answer()
    await callback.message.answer("Главное меню:", reply_markup=get_main_keyboard())

# ============================================================
# ОБРАБОТЧИКИ НАСТРОЕК ПЛАТЕЖЕЙ (INLINE)
# ============================================================

@dp.message(F.text == "⚙️ Настройки платежей")
async def admin_payment_settings(message: types.Message):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    await show_payment_settings(message)

@dp.callback_query(F.data == "payment_toggle")
async def payment_toggle(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    current = await is_payments_enabled()
    await set_setting('payments_enabled', 'false' if current else 'true')
    await callback.answer("✅ Статус изменён")
    await show_payment_settings(callback, edit=True)

@dp.callback_query(F.data == "payment_check_tokens")
async def payment_check_tokens(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    live_status = "✅ присутствует" if PROVIDER_TOKEN_LIVE else "❌ отсутствует"
    test_status = "✅ присутствует" if PROVIDER_TOKEN_TEST else "❌ отсутствует"
    text = (
        f"🔍 Проверка токенов:\n\n"
        f"LIVE токен: {live_status}\n"
        f"TEST токен: {test_status}\n\n"
        f"Текущий статус платежей: {'✅ ВКЛЮЧЕН' if await is_payments_enabled() else '❌ ВЫКЛЮЧЕН'}"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="payment_back")]
        ]
    )
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "payment_change_price")
async def payment_change_price(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    await callback.message.edit_text(
        "💰 Введите новую цену разбора в рублях (только число):\n"
        "Например: 7000\n\n"
        "Для отмены отправьте /cancel"
    )
    await state.set_state(OrderStates.waiting_new_price)
    await state.update_data(return_to_payment_settings=True)
    await callback.answer()

@dp.callback_query(F.data == "payment_back")
async def payment_back(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    await callback.message.delete()
    await callback.message.answer("Возврат в админ-панель.", reply_markup=get_admin_keyboard())
    await callback.answer()

# ============================================================
# ОБРАБОТЧИК ИЗМЕНЕНИЯ ЦЕНЫ (переиспользуем существующий, но с возвратом в настройки)
# ============================================================

@dp.message(StateFilter(OrderStates.waiting_new_price))
async def process_new_price(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    data = await state.get_data()
    return_to_settings = data.get('return_to_payment_settings', False)
    try:
        new_price = int(message.text.strip())
        if new_price <= 0:
            await message.answer("❌ Цена должна быть больше 0.")
            return
    except ValueError:
        await message.answer("❌ Введите число (например, 7000)")
        return
    await set_setting('price', str(new_price))
    await message.answer(f"✅ Цена успешно изменена на {new_price} ₽")
    await state.clear()
    
    if return_to_settings:
        await show_payment_settings(message)
    else:
        await message.answer("Главное меню:", reply_markup=get_main_keyboard())

# ============================================================
# ПЛАТЁЖНАЯ ЧАСТЬ (pre_checkout и successful_payment)
# ============================================================

@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: PreCheckoutQuery):
    payload = pre_checkout_query.invoice_payload
    if payload.startswith('bazi_'):
        parts = payload.split('_')
        if len(parts) >= 2:
            try:
                order_id = int(parts[1])
                order = await get_order(order_id)
                if order and order['status'] != 'pending_payment':
                    await bot.answer_pre_checkout_query(
                        pre_checkout_query.id,
                        ok=False,
                        error_message="Этот заказ уже неактивен. Пожалуйста, создайте новый заказ."
                    )
                    return
            except:
                pass
    await bot.answer_pre_checkout_query(
        pre_checkout_query.id,
        ok=True,
        error_message="Извините, произошла ошибка. Попробуйте позже."
    )

@dp.message(F.successful_payment)
async def successful_payment_handler(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id == bot.id:
        await message.answer("⛔ Бот не может получать оплату.")
        return
    await ensure_user_exists(
        user_id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name
    )

    payment = message.successful_payment
    payload = payment.invoice_payload

    if payload.startswith('gift_'):
        # Парсим pending_id и user_id из payload
        parts = payload.split('_')
        if len(parts) >= 2:
            pending_id = int(parts[1])
        else:
            await message.answer("❌ Ошибка: неверный идентификатор платежа.")
            return

        # Получаем данные из БД
        async with db_pool.acquire() as conn:
            record = await conn.fetchrow('SELECT * FROM gift_certificates WHERE id = $1', pending_id)
            if not record or record['status'] != 'pending':
                await message.answer("❌ Сертификат не найден или уже обработан.")
                return
            amount = record['amount']
            gift_message = record['gift_message']
            purchaser_id = record['purchaser_id']

        # Генерируем уникальный код
        prefix = await get_setting('cert_code_prefix', 'GIFT-')
        length = int(await get_setting('cert_code_length', '8'))
        while True:
            code = generate_gift_code(prefix=prefix, length=length)
            async with db_pool.acquire() as conn:
                existing = await conn.fetchval('SELECT id FROM gift_certificates WHERE code = $1', code)
                if not existing:
                    break

        # Обновляем запись: добавляем код, баланс, статус
        async with db_pool.acquire() as conn:
            await conn.execute('''
                UPDATE gift_certificates
                SET code = $1, balance = $2, status = 'active', used_at = NULL
                WHERE id = $3
            ''', code, amount, pending_id)

        await notify_admin_gift_purchase(user_id, amount, code, gift_message)

        try:
            image_path, pdf_path = await generate_certificate_files(code, amount)
        except Exception as e:
            await message.answer(f"❌ Ошибка генерации сертификата: {e}")
            return

        caption = f"🎁 Поздравляем! Вы получили подарочный сертификат на сумму {amount} ₽.\n\n"
        if gift_message:
            caption += f"💌 Поздравление от отправителя:\n<blockquote>{gift_message}</blockquote>\n\n"
        caption += (
            f"Код сертификата:\n"
            f"<code>{code}</code>\n\n"
            f"✅ Используйте этот код при оформлении заказа.\n\n"
            f"🔗 Чтобы активировать сертификат: <a href=\"https://t.me/{BOT_USERNAME}?start=gift_{code}\">перейдите по этой ссылке</a>"
        )
        await bot.send_photo(user_id, FSInputFile(image_path), caption=caption, parse_mode="HTML")

        if pdf_path and os.path.exists(pdf_path):
            await bot.send_document(user_id, FSInputFile(pdf_path), caption="📄 Версия для печати")

        instruction_text = (
            "Если вы хотите подарить этот сертификат, просто перешлите два верхних сообщения "
            "(с изображением и с PDF-файлом) вашему получателю. Он сможет активировать сертификат "
            "по ссылке в первом сообщении."
        )
        await bot.send_message(user_id, instruction_text, reply_markup=get_main_keyboard())

        await state.clear()

    else:
        payload_parts = payload.split('_')
        if len(payload_parts) >= 2 and payload_parts[0] == 'bazi':
            order_id = int(payload_parts[1])
        else:
            orders = await get_orders_by_user(user_id)
            pending = [o for o in orders if o['status'] == 'pending_payment']
            if pending:
                order_id = pending[0]['id']
            else:
                await message.answer("❌ Не удалось определить заказ. Обратитесь к администратору.")
                return

        order = await get_order(order_id)
        if not order or order['status'] != 'pending_payment':
            await message.answer(
                "❌ Этот заказ уже неактивен или был отменён. Пожалуйста, создайте новый заказ."
            )
            return

        await save_payment_history(order_id, user_id, payment.total_amount, payload)
        await update_order_status(order_id, 'paid')

        if order['gift_code'] and order['gift_amount_used'] > 0:
            await update_gift_balance_and_status(order['gift_code'], order['gift_amount_used'], order_id, user_id)

        user = await get_user(user_id)
        await notify_admin_new_order(order_id, user)
        await state.clear()
        await message.answer(
            "✅ Оплата успешно получена!\n\n"
            f"Ваш заказ №{order_id} поставлен в очередь на обработку. Как только специалист возьмёт его в работу, вы получите уведомление."
        )
        await message.answer(
            "📌 Вы можете заказать новый разбор или проверить статус существующих заказов.",
            reply_markup=get_status_button_keyboard()
        )

# ============================================================
# ОСТАЛЬНЫЕ ОБРАБОТЧИКИ
# ============================================================

@dp.callback_query(F.data == "check_status")
async def check_status(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id == bot.id:
        await callback.answer("⛔ Бот не может проверять статус.", show_alert=True)
        return
    active_orders = await get_active_orders_for_user(user_id)
    if not active_orders:
        await callback.message.answer("❌ У вас нет активных заказов.")
        await callback.answer()
        return
    status_text = "📋 Ваши активные заказы:\n\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for order in active_orders:
        status_emoji = {
            'pending': '🆕', 'pending_payment': '⏳', 'paid': '✅', 'processing': '🔨'
        }.get(order['status'], '❓')
        status_desc = {
            'pending': 'Ожидает оплаты',
            'pending_payment': 'Ожидает оплаты',
            'paid': 'Оплачен, ожидает начала',
            'processing': 'В процессе подготовки'
        }.get(order['status'], 'Неизвестный статус')
        created_local = (order['created_at'] + timedelta(hours=3)).strftime('%d.%m.%Y %H:%M') + " (МСК)" if order['created_at'] else '—'
        status_text += f"{status_emoji} Заказ №{order['id']}: {status_desc}\n"
        status_text += f"   📅 Создан: {created_local}\n"
        if order['status'] in ('pending', 'pending_payment'):
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(
                    text=f"💳 Оплатить №{order['id']}",
                    callback_data=f"pay_order_{order['id']}"
                ),
                InlineKeyboardButton(
                    text=f"❌ Отменить №{order['id']}",
                    callback_data=f"cancel_order_{order['id']}"
                )
            ])
    status_text += "\nВыберите действие:"
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="📊 Проверить статусы заказов", callback_data="check_status")])
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="✅ Создать ещё один заказ", callback_data="create_new_order")])
    await callback.message.answer(status_text, reply_markup=keyboard)
    await callback.answer()

@dp.message(F.text == "ℹ️ О сервисе")
async def about(message: types.Message):
    price = await get_price()
    text = (
        f"<b>О сервисе BaziExpert</b>\n\n"
        "🌟 Здравствуйте! Добро пожаловать в BaziExpert!\n\n"
        "Что такое Ба Цзы?\n"
        "Ба Цзы (Четыре Столпа Судьбы) — это древняя китайская метафизическая система, которая по дате, времени и месту вашего рождения строит вашу уникальную карту личности и судьбы.\n" 
        "Она анализирует взаимодействие Небесных Стволов и Земных Ветвей — десяти энергетических столпов, формирующих вашу личность, судьбу и жизненные циклы.\n\n"
        "Наш специалист построит вашу персональную карту Ба Цзы и сделает для вас подробный многостраничный разбор, в котором вы узнаете:\n\n"
        "🧘‍♂️ Хозяина Дня — ваш главный элемент личности\n"
        "⚖️ Баланс 5 элементов в вашей карте\n"
        "💪 Сильные и слабые стороны, ваш потенциал и зоны роста\n"
        "🛡️ Уязвимости и защитные механизмы\n"
        "🎭 Архетип личности и особенности характера\n"
        "📈 Анализ десятилетних тактов (столпов удачи) — периоды взлётов и спадов\n"
        "🔮 Практические советы, как сбалансировать элементы и улучшить качество жизни\n"
        "🌿 Прогноз удачных и неудачных периодов на ближайшие 10 лет\n\n"
        "Разбор будет оформлен в красиво стилизованном PDF-документе, который можно сохранить и перечитывать в любое время.\n\n"
        f"💰 Стоимость разбора: {price} ₽\n\n"
        "Готовы узнать свою судьбу? Нажмите кнопку ниже, чтобы заказать разбор!\n\n"
        "📄 <a href=\"https://telegra.ph/PUBLICHNAYA-OFERTA-POLZOVATELSKOE-SOGLASHENIE-07-14\">Пользовательское соглашение</a>\n"
        "🔒 <a href=\"https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-07-14-83\">Политика конфиденциальности</a>"
    )
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)

@dp.message(F.text == "📞 Контакты")
async def contacts(message: types.Message):
    await message.answer(
        "📞 Контакты:\n✉️ Админ: @Artem_001_88\n📧 Email: baziexpert_bot@mail.ru\n💬 Чат поддержки: https://t.me/+ivPVuZXMcfQyYWYy", disable_web_page_preview=True
    )

async def notify_admin_new_order(order_id: int, user: Record):
    try:
        order = await get_order(order_id)
        if not order:
            logger.error(f"Заказ {order_id} не найден при отправке уведомления")
            return
        local_created = (order['created_at'] + timedelta(hours=3)).strftime('%d.%m.%Y %H:%M') + " (МСК)" if order['created_at'] else '—'
        text = (
            f"🆕 НОВЫЙ ЗАКАЗ №{order_id}!\n\n"
            f"👤 Имя (в заказе): {order['client_name'] or 'Не указано'}\n"
            f"👤 Telegram: {user['first_name']} {user['last_name'] or ''} (@{user['username'] or 'нет'})\n"
            f"🆔 ID пользователя: {user['tg_id']}\n"
            f"📅 Дата рождения: {order['birth_date']} в {order['birth_time']}\n"
            f"🏙️ Место рождения: {order['birth_city']}\n"
            f"👤 Пол: {'Мужской' if order['gender'] == 'male' else 'Женский'}\n"
            f"📧 Email: {order['email'] or 'не указан'}\n"
            f"⏰ Время заказа: {local_created}\n"
            "Статус: ОПЛАЧЕН"
        )
        keyboard = get_take_order_keyboard(order_id)
        for admin_id in ADMIN_IDS:
            if admin_id == bot.id:
                continue
            try:
                await bot.send_message(admin_id, text, reply_markup=keyboard)
            except Exception as e:
                logger.error(f"Ошибка уведомления админа {admin_id}: {e}")
    except Exception as e:
        logger.error(f"Ошибка в notify_admin_new_order: {e}")

async def notify_admin_gift_purchase(purchaser_id: int, amount: int, code: str, gift_message: str = None):
    try:
        user = await get_user(purchaser_id)
        if user:
            user_name = f"{user['first_name']} {user['last_name'] or ''}".strip() or "Неизвестно"
            username = f"@{user['username']}" if user.get('username') else "нет"
        else:
            user_name = "Неизвестно"
            username = "нет"

        text = (
            f"🎁 НОВАЯ ПОКУПКА СЕРТИФИКАТА!\n\n"
            f"💰 Сумма: {amount} ₽\n"
            f"🔑 Код: <code>{code}</code>\n"
            f"👤 Покупатель: {user_name} (ID: {purchaser_id}, {username})\n"
        )
        if gift_message:
            text += f"💌 Поздравление: «{gift_message}»\n"
        text += f"📅 Дата покупки: {datetime.now().strftime('%d.%m.%Y %H:%M')} (МСК)"

        for admin_id in ADMIN_IDS:
            if admin_id == bot.id:
                continue
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Ошибка уведомления админа {admin_id} о покупке сертификата: {e}")
    except Exception as e:
        logger.error(f"Ошибка в notify_admin_gift_purchase: {e}")

@dp.message(F.text == "📋 Все заявки")
async def admin_all_orders(message: types.Message):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    async with db_pool.acquire() as conn:
        orders = await conn.fetch('''
            SELECT o.*, u.tg_id, u.first_name, u.username
            FROM orders o
            JOIN users u ON o.user_id = u.tg_id
            ORDER BY o.created_at DESC
            LIMIT 20
        ''')
    if not orders:
        await message.answer("📭 Заявок нет.")
        return
    text = "📋 Все заявки (последние 20):\n\n"
    for order in orders:
        status_emoji = {
            'pending': '🆕', 'pending_payment': '⏳', 'paid': '✅',
            'processing': '🔨', 'done': '📄', 'cancelled': '🚫'
        }.get(order['status'], '❓')
        local_created = (order['created_at'] + timedelta(hours=3)).strftime('%d.%m.%Y %H:%M') + " (МСК)" if order['created_at'] else '—'
        file_info = f"📁 Файл: {'загружен' if order['file_id'] else 'не загружен'}" if order['status'] == 'done' else ""
        text += (
            f"{status_emoji} Заказ №{order['id']} | {order['first_name'] or 'Без имени'} (ID: {order['tg_id']})\n"
            f"   👤 Имя в заказе: {order['client_name'] or 'Не указано'}\n"
            f"   Дата заказа: {local_created} | Статус: {order['status']}\n"
            f"   {file_info}\n\n"
        )
    await message.answer(text)

@dp.message(F.text == "⏳ Новые заявки")
async def admin_new_orders(message: types.Message):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    orders = await get_orders_by_status('paid')
    if not orders:
        await message.answer("🆕 Новых оплаченных заявок нет.")
        return
    for order in orders:
        user = await get_user(order['user_id'])
        if not user:
            continue
        order_id = order['id']
        local_created = (order['created_at'] + timedelta(hours=3)).strftime('%d.%m.%Y %H:%M') + " (МСК)" if order['created_at'] else '—'
        text = (
            f"🆕 Заказ №{order_id}\n\n"
            f"👤 Telegram: {user['first_name']} {user['last_name'] or ''} (@{user['username'] or 'нет'})\n"
            f"📅 Дата рождения: {order['birth_date']} в {order['birth_time']}\n"
            f"🏙️ Место рождения: {order['birth_city']}\n"
            f"👤 Пол: {'Мужской' if order['gender'] == 'male' else 'Женский'}\n"
            f"👤 Имя в заказе: {order['client_name'] or 'Не указано'}\n"
            f"📧 Email: {order['email'] or 'не указан'}\n"
            f"📅 Заказано: {local_created}"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Взять в работу", callback_data=f"take_{order_id}")]
            ]
        )
        await message.answer(text, reply_markup=keyboard)

@dp.callback_query(F.data.startswith("take_"))
async def take_order_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("⛔ Нет прав", show_alert=True)
        return
    order_id = int(callback.data.split("_")[1])
    order = await get_order(order_id)
    if not order or order['status'] != 'paid':
        await callback.answer("❌ Заказ уже взят или неактивен", show_alert=True)
        return
    await update_order_status(order_id, 'processing')
    user = await get_user(order['user_id'])
    if user:
        try:
            await bot.send_message(
                user['tg_id'],
                f"🔨 Специалист взял ваш заказ №{order_id} в работу."
            )
        except Exception as e:
            logger.error(f"Ошибка уведомления клиента {user['tg_id']}: {e}")
    await callback.message.edit_text(
        f"✅ Заказ №{order_id} взят в работу! Клиент уведомлён.",
        reply_markup=None
    )
    await callback.answer("✅ Заказ взят в работу!")

@dp.message(F.text == "📋 Текущий заказ")
async def admin_current_order(message: types.Message):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    orders = await get_orders_by_status('processing')
    if not orders:
        await message.answer("📭 Нет заказов в работе.")
        return
    for order in orders:
        user = await get_user(order['user_id'])
        if not user:
            continue
        local_created = (order['created_at'] + timedelta(hours=3)).strftime('%d.%m.%Y %H:%M') + " (МСК)" if order['created_at'] else '—'
        text = (
            f"🔨 Текущий заказ №{order['id']}\n\n"
            f"👤 Telegram: {user['first_name']} {user['last_name'] or ''} (@{user['username'] or 'нет'})\n"
            f"📅 Дата рождения: {order['birth_date']} в {order['birth_time']}\n"
            f"🏙️ Место рождения: {order['birth_city']}\n"
            f"👤 Пол: {'Мужской' if order['gender'] == 'male' else 'Женский'}\n"
            f"👤 Имя в заказе: {order['client_name'] or 'Не указано'}\n"
            f"📧 Email: {order['email'] or 'не указан'}\n"
            f"📅 Заказано: {local_created}"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📤 Отправить файл", callback_data=f"send_file_{order['id']}")]
            ]
        )
        await message.answer(text, reply_markup=keyboard)

@dp.callback_query(F.data.startswith("send_file_"))
async def admin_send_file(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("⛔ Нет прав", show_alert=True)
        return
    order_id = int(callback.data.split("_")[2])
    order = await get_order(order_id)
    if not order:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    if order['status'] != 'processing':
        await callback.answer("Заказ не в статусе 'в работе'", show_alert=True)
        return
    await state.update_data(upload_order_id=order_id)
    await callback.message.answer(
        f"📤 Отправьте файл для заказа №{order_id} (PDF, DOC, DOCX, TXT):"
    )
    await state.set_state(OrderStates.admin_waiting_file_upload)
    await callback.answer()

@dp.message(StateFilter(OrderStates.admin_waiting_file_upload), F.document)
async def admin_upload_file_for_order(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    data = await state.get_data()
    order_id = data.get('upload_order_id')
    if not order_id:
        await message.answer("❌ Ошибка: не найден номер заказа. Попробуйте снова через 'Текущий заказ'.")
        await state.clear()
        return
    doc = message.document
    if not doc.file_name.endswith(('.pdf', '.doc', '.docx', '.txt')):
        await message.answer("❌ Поддерживаются только PDF, DOC, DOCX, TXT")
        return
    if doc.file_size > 50 * 1024 * 1024:
        await message.answer("❌ Файл слишком большой (макс 50 МБ)")
        return

    file_id = doc.file_id
    file_name = doc.file_name

    await save_order_file(order_id, file_id, file_name)

    order = await get_order(order_id)
    user = await get_user(order['user_id']) if order else None
    if user:
        await bot.send_document(
            user['tg_id'],
            file_id,
            caption=f"📄 Ваш разбор по заказу №{order_id} готов! Благодарим за доверие."
        )
        await bot.send_message(
            user['tg_id'],
            "Если хотите заказать ещё один или приобрести подарочный сертификат – воспользуйтесь меню ниже.",
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer("❌ Пользователь не найден")
        return

    await anonymize_order(order_id)
    await message.answer(f"✅ Файл отправлен по заказу №{order_id}.\n📁 File ID: {file_id}")
    await state.clear()

@dp.message(F.text == "🎁 Сертификаты")
async def admin_certificates_menu(message: types.Message):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    await message.answer(
        "📁 Управление сертификатами:",
        reply_markup=get_certificates_menu_keyboard()
    )

@dp.message(F.text == "🔙 Назад")
async def back_to_admin_panel(message: types.Message):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    await message.answer(
        "👋 Возврат в админ-панель.",
        reply_markup=get_admin_keyboard()
    )

@dp.message(F.text == "🎨 Шаблоны сертификатов")
async def admin_templates(message: types.Message):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    templates = await get_all_templates()
    if not templates:
        text = "📭 Шаблонов пока нет.\n"
    else:
        text = "📋 Список шаблонов:\n\n"
        for t in templates:
            text += f"💰 {t['amount']} ₽ → {t['file_path']}\n"
    text += "\nВыберите действие:"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить шаблон", callback_data="admin_add_template")],
            [InlineKeyboardButton(text="❌ Удалить шаблон", callback_data="admin_delete_template")]
        ]
    )
    await message.answer(text, reply_markup=keyboard)

@dp.callback_query(F.data == "admin_add_template")
async def admin_add_template_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "Введите сумму (в рублях), для которой будет использоваться этот шаблон:\n"
        "Например: 1000"
    )
    await state.set_state(OrderStates.admin_waiting_template_amount)
    await callback.answer()

@dp.message(StateFilter(OrderStates.admin_waiting_template_amount))
async def admin_add_template_amount(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    try:
        amount = int(message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите положительное целое число.")
        return
    await state.update_data(template_amount=amount)
    await message.answer(
        f"💰 Сумма: {amount} ₽.\n"
        "Теперь отправьте изображение шаблона (PNG или JPEG).\n"
        "Для отмены отправьте /cancel"
    )
    await state.set_state(OrderStates.admin_waiting_template_file)

@dp.message(StateFilter(OrderStates.admin_waiting_template_file), F.photo | F.document)
async def admin_add_template_file(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    data = await state.get_data()
    amount = data.get('template_amount')
    if not amount:
        await message.answer("❌ Ошибка: сумма не найдена. Начните заново.")
        await state.clear()
        return
    if message.photo:
        file_id = message.photo[-1].file_id
        ext = 'png'
    elif message.document:
        doc = message.document
        if not doc.file_name.lower().endswith(('.png', '.jpg', '.jpeg')):
            await message.answer("❌ Поддерживаются только PNG и JPEG.")
            return
        file_id = doc.file_id
        ext = doc.file_name.split('.')[-1]
    else:
        await message.answer("❌ Пожалуйста, отправьте изображение.")
        return
    file = await bot.get_file(file_id)
    os.makedirs('media/templates', exist_ok=True)
    file_name = f"template_{amount}.{ext}"
    file_path = f"media/templates/{file_name}"
    await bot.download_file(file.file_path, file_path)
    await add_template(amount, file_path)
    await message.answer(f"✅ Шаблон для {amount} ₽ успешно загружен!")
    await state.clear()
    await admin_templates(message)

@dp.callback_query(F.data == "admin_delete_template")
async def admin_delete_template_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    templates = await get_all_templates()
    if not templates:
        await callback.message.edit_text("📭 Нет шаблонов для удаления.")
        await callback.answer()
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for t in templates:
        keyboard.inline_keyboard.append(
            [InlineKeyboardButton(text=f"❌ {t['amount']} ₽", callback_data=f"admin_del_template_{t['amount']}")]
        )
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_templates_back")])
    await callback.message.edit_text("Выберите шаблон для удаления:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_del_template_"))
async def admin_delete_template_confirm(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    amount = int(callback.data.split("_")[3])
    await state.update_data(del_amount=amount)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"admin_del_template_confirm_{amount}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_templates_back")]
        ]
    )
    await callback.message.edit_text(f"Удалить шаблон для {amount} ₽?", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_del_template_confirm_"))
async def admin_delete_template_final(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    data = await state.get_data()
    amount = data.get('del_amount')
    if not amount:
        await callback.message.edit_text("❌ Ошибка: не удалось определить шаблон для удаления.")
        await callback.answer()
        return
    template_path = await get_template_path(amount)
    if template_path and os.path.exists(template_path):
        os.remove(template_path)
    await delete_template(amount)
    await callback.message.edit_text(f"✅ Шаблон для {amount} ₽ удалён.")
    await state.clear()
    await admin_templates(callback.message)
    await callback.answer()

@dp.callback_query(F.data == "admin_templates_back")
async def admin_templates_back(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    await admin_templates(callback.message)
    await callback.answer()

@dp.message(F.text == "⚙️ Настройки сертификатов")
async def admin_cert_settings(message: types.Message):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    settings = await get_cert_settings()
    text = (
        f"⚙️ Текущие настройки сертификатов:\n\n"
        f"📐 Положение текста: x={settings['x']}, y={settings['y']} (центр)\n"
        f"🔤 Размер шрифта: {settings['font_size']}\n"
        f"🎨 Цвет текста: {','.join(map(str, settings['font_color']))}\n"
        f"📁 Активный шрифт: {settings['font_path']}\n"
        f"🖼️ Шаблон по умолчанию: {settings['default_template']}\n"
        f"🔑 Префикс кода: {settings['prefix']}\n"
        f"🔢 Длина кода: {settings['length']}\n\n"
        "Выберите параметр для изменения:"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📐 Положение X", callback_data="set_cert_x"),
             InlineKeyboardButton(text="📐 Положение Y", callback_data="set_cert_y")],
            [InlineKeyboardButton(text="🔤 Размер шрифта", callback_data="set_cert_font_size"),
             InlineKeyboardButton(text="🎨 Цвет текста", callback_data="set_cert_font_color")],
            [InlineKeyboardButton(text="📁 Шрифты", callback_data="manage_fonts")],
            [InlineKeyboardButton(text="🖼️ Шаблон по умолчанию", callback_data="set_cert_template_default"),
             InlineKeyboardButton(text="🔑 Префикс кода", callback_data="set_cert_code_prefix")],
            [InlineKeyboardButton(text="🔢 Длина кода", callback_data="set_cert_code_length")]
        ]
    )
    await message.answer(text, reply_markup=keyboard)

@dp.message(F.text == "📱 Настройки QR")
async def admin_qr_settings(message: types.Message):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    qr_img = await get_qr_image_settings()
    qr_pdf = await get_qr_pdf_settings()
    img_status = "✅ включён" if qr_img['enabled'] else "❌ выключен"
    pdf_status = "✅ включён" if qr_pdf['enabled'] else "❌ выключен"
    text = (
        f"📱 Настройки QR-кода:\n\n"
        f"🔹 На изображении:\n"
        f"   Статус: {img_status}\n"
        f"   X: {qr_img['x']}  Y: {qr_img['y']}  Размер: {qr_img['size']} px\n\n"
        f"🔹 В PDF (вторая страница):\n"
        f"   Статус: {pdf_status}\n"
        f"   X центр: {qr_pdf['x']}  Y центр: {qr_pdf['y']}  Размер: {qr_pdf['size']} pts\n"
        f"   Подпись: «{qr_pdf['label']}» (шрифт {qr_pdf['label_font_size']})\n\n"
        "Выберите раздел для изменения:"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🖼️ На изображении", callback_data="qr_settings_image")],
            [InlineKeyboardButton(text="📄 В PDF", callback_data="qr_settings_pdf")]
        ]
    )
    await message.answer(text, reply_markup=keyboard)

@dp.callback_query(F.data == "qr_settings_image")
async def qr_settings_image(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    qr = await get_qr_image_settings()
    status = "✅ включён" if qr['enabled'] else "❌ выключен"
    text = (
        f"🖼️ Настройки QR на изображении:\n\n"
        f"Статус: {status}\n"
        f"Положение X: {qr['x']}\n"
        f"Положение Y: {qr['y']}\n"
        f"Размер: {qr['size']} px\n\n"
        "Выберите параметр:"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔘 Включить/выключить", callback_data="qr_image_toggle")],
            [InlineKeyboardButton(text="📐 X", callback_data="qr_image_set_x"),
             InlineKeyboardButton(text="📐 Y", callback_data="qr_image_set_y")],
            [InlineKeyboardButton(text="📏 Размер", callback_data="qr_image_set_size")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="qr_settings_back")]
        ]
    )
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "qr_settings_pdf")
async def qr_settings_pdf(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    qr = await get_qr_pdf_settings()
    status = "✅ включён" if qr['enabled'] else "❌ выключен"
    text = (
        f"📄 Настройки QR в PDF (вторая страница):\n\n"
        f"Статус: {status}\n"
        f"Центр X: {qr['x']}\n"
        f"Центр Y: {qr['y']}\n"
        f"Размер: {qr['size']} pts\n"
        f"Подпись: «{qr['label']}»\n"
        f"Размер шрифта подписи: {qr['label_font_size']}\n\n"
        "Выберите параметр:"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔘 Включить/выключить", callback_data="qr_pdf_toggle")],
            [InlineKeyboardButton(text="📐 X центра", callback_data="qr_pdf_set_x"),
             InlineKeyboardButton(text="📐 Y центра", callback_data="qr_pdf_set_y")],
            [InlineKeyboardButton(text="📏 Размер", callback_data="qr_pdf_set_size")],
            [InlineKeyboardButton(text="✏️ Подпись", callback_data="qr_pdf_set_label")],
            [InlineKeyboardButton(text="🔤 Размер подписи", callback_data="qr_pdf_set_label_font_size")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="qr_settings_back")]
        ]
    )
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "qr_settings_back")
async def qr_settings_back(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    await admin_qr_settings(callback.message)
    await callback.answer()

@dp.callback_query(F.data == "qr_image_toggle")
async def qr_image_toggle(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    qr = await get_qr_image_settings()
    new_state = not qr['enabled']
    await set_qr_image_enabled(new_state)
    await callback.message.edit_text(f"✅ QR на изображении {'включён' if new_state else 'выключен'}.")
    await callback.answer()

@dp.callback_query(F.data.startswith("qr_image_set_"))
async def qr_image_set_param(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    param_map = {
        'qr_image_set_x': 'qr_image_x',
        'qr_image_set_y': 'qr_image_y',
        'qr_image_set_size': 'qr_image_size'
    }
    param = param_map.get(callback.data)
    if not param:
        await callback.answer("Неизвестный параметр", show_alert=True)
        return
    current = await get_setting(param, '0')
    await state.update_data(setting_key=param)
    await callback.message.edit_text(
        f"Введите новое значение для {param} (текущее: {current}):\n"
        "Для отмены отправьте /cancel"
    )
    await state.set_state(OrderStates.admin_waiting_setting_value)
    await callback.answer()

@dp.callback_query(F.data == "qr_pdf_toggle")
async def qr_pdf_toggle(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    qr = await get_qr_pdf_settings()
    new_state = not qr['enabled']
    await set_qr_pdf_enabled(new_state)
    await callback.message.edit_text(f"✅ QR в PDF {'включён' if new_state else 'выключен'}.")
    await callback.answer()

@dp.callback_query(F.data.startswith("qr_pdf_set_"))
async def qr_pdf_set_param(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    param_map = {
        'qr_pdf_set_x': 'qr_pdf_x',
        'qr_pdf_set_y': 'qr_pdf_y',
        'qr_pdf_set_size': 'qr_pdf_size',
        'qr_pdf_set_label': 'qr_pdf_label',
        'qr_pdf_set_label_font_size': 'qr_pdf_label_font_size'
    }
    param = param_map.get(callback.data)
    if not param:
        await callback.answer("Неизвестный параметр", show_alert=True)
        return
    current = await get_setting(param, '')
    await state.update_data(setting_key=param)
    await callback.message.edit_text(
        f"Введите новое значение для {param} (текущее: {current}):\n"
        "Для отмены отправьте /cancel"
    )
    await state.set_state(OrderStates.admin_waiting_setting_value)
    await callback.answer()

@dp.callback_query(F.data == "manage_fonts")
async def admin_fonts_menu(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    current_font = await get_setting('cert_font_path', 'Не задан')
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Выбрать шрифт", callback_data="fonts_select")],
            [InlineKeyboardButton(text="📤 Загрузить шрифт", callback_data="fonts_upload")],
            [InlineKeyboardButton(text="🗑️ Удалить шрифт", callback_data="fonts_delete")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_cert_settings_back")]
        ]
    )
    await callback.message.edit_text(
        f"📁 Управление шрифтами\n\n"
        f"Текущий активный шрифт: {current_font}\n\n"
        "Выберите действие:",
        reply_markup=keyboard
    )
    await callback.answer()

@dp.callback_query(F.data == "fonts_select")
async def fonts_select_menu(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    fonts = await get_fonts()
    if not fonts:
        await callback.message.edit_text(
            "📭 Нет загруженных шрифтов. Загрузите шрифт через '📤 Загрузить шрифт'.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="manage_fonts")]]
            )
        )
        await callback.answer()
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for f in fonts:
        keyboard.inline_keyboard.append(
            [InlineKeyboardButton(text=f"{f['name']}", callback_data=f"font_select_{f['id']}")]
        )
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="manage_fonts")])
    await callback.message.edit_text(
        "Выберите шрифт для активации:",
        reply_markup=keyboard
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("font_select_"))
async def font_select_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    font_id = int(callback.data.split('_')[2])
    font = await get_font_by_id(font_id)
    if not font:
        await callback.answer("Шрифт не найден", show_alert=True)
        return
    await set_active_font(font['file_path'])
    await callback.message.edit_text(
        f"✅ Активный шрифт изменён на: {font['name']}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад в меню шрифтов", callback_data="manage_fonts")]]
        )
    )
    await callback.answer()

@dp.callback_query(F.data == "fonts_upload")
async def fonts_upload_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    await callback.message.edit_text(
        "Отправьте файл шрифта (поддерживаются .ttf и .otf, макс. 5 МБ).\n"
        "Для отмены отправьте /cancel"
    )
    await state.set_state(OrderStates.admin_waiting_font_file)
    await callback.answer()

@dp.message(StateFilter(OrderStates.admin_waiting_font_file), F.document)
async def admin_upload_font(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    doc = message.document
    if not doc.file_name.lower().endswith(('.ttf', '.otf')):
        await message.answer("❌ Поддерживаются только .ttf и .otf файлы.")
        return
    if doc.file_size > 5 * 1024 * 1024:
        await message.answer("❌ Файл слишком большой (макс 5 МБ).")
        return
    file = await bot.get_file(doc.file_id)
    os.makedirs('fonts', exist_ok=True)
    base, ext = os.path.splitext(doc.file_name)
    file_path = f"fonts/{doc.file_name}"
    counter = 1
    while os.path.exists(file_path):
        file_path = f"fonts/{base}_{counter}{ext}"
        counter += 1
    await bot.download_file(file.file_path, file_path)
    font_id = await add_font(doc.file_name, file_path)
    await set_active_font(file_path)
    await message.answer(
        f"✅ Шрифт '{doc.file_name}' загружен и установлен как активный!\n"
        f"Путь: {file_path}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад в меню шрифтов", callback_data="manage_fonts")]]
        )
    )
    await state.clear()

@dp.callback_query(F.data == "fonts_delete")
async def fonts_delete_menu(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    fonts = await get_fonts()
    if not fonts:
        await callback.message.edit_text(
            "📭 Нет загруженных шрифтов для удаления.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="manage_fonts")]]
            )
        )
        await callback.answer()
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for f in fonts:
        keyboard.inline_keyboard.append(
            [InlineKeyboardButton(text=f"❌ {f['name']}", callback_data=f"font_delete_{f['id']}")]
        )
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="manage_fonts")])
    await callback.message.edit_text(
        "Выберите шрифт для удаления:",
        reply_markup=keyboard
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("font_delete_"))
async def font_delete_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    font_id = int(callback.data.split('_')[2])
    font = await get_font_by_id(font_id)
    if not font:
        await callback.answer("Шрифт не найден", show_alert=True)
        return
    current_active = await get_setting('cert_font_path')
    is_active = (current_active == font['file_path'])
    if os.path.exists(font['file_path']):
        os.remove(font['file_path'])
    await delete_font(font_id)
    if is_active:
        last_font = await get_last_font()
        if last_font:
            await set_active_font(last_font['file_path'])
            await callback.message.edit_text(
                f"✅ Шрифт '{font['name']}' удалён.\n"
                f"Активным стал: {last_font['name']}",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад в меню шрифтов", callback_data="manage_fonts")]]
                )
            )
        else:
            default_font = 'fonts/arial.ttf'
            await set_active_font(default_font)
            await callback.message.edit_text(
                f"✅ Шрифт '{font['name']}' удалён.\n"
                "Активным установлен стандартный шрифт: fonts/arial.ttf",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад в меню шрифтов", callback_data="manage_fonts")]]
                )
            )
    else:
        await callback.message.edit_text(
            f"✅ Шрифт '{font['name']}' удалён.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад в меню шрифтов", callback_data="manage_fonts")]]
            )
        )
    await callback.answer()

@dp.callback_query(F.data == "admin_cert_settings_back")
async def admin_cert_settings_back(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    await admin_cert_settings(callback.message)
    await callback.answer()

@dp.callback_query(F.data.startswith("set_cert_"))
async def admin_set_setting_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    key_map = {
        'set_cert_x': 'cert_x',
        'set_cert_y': 'cert_y',
        'set_cert_font_size': 'cert_font_size',
        'set_cert_font_color': 'cert_font_color',
        'set_cert_template_default': 'cert_template_default',
        'set_cert_code_prefix': 'cert_code_prefix',
        'set_cert_code_length': 'cert_code_length',
    }
    key = key_map.get(callback.data)
    if not key:
        await callback.answer("Неизвестная настройка", show_alert=True)
        return
    current = await get_setting(key, 'не задано')
    await state.update_data(setting_key=key)
    await callback.message.edit_text(
        f"Введите новое значение для {key} (текущее: {current}):\n"
        "Для отмены отправьте /cancel"
    )
    await state.set_state(OrderStates.admin_waiting_setting_value)
    await callback.answer()

@dp.message(StateFilter(OrderStates.admin_waiting_setting_value))
async def admin_set_setting_value(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    data = await state.get_data()
    key = data.get('setting_key')
    if not key:
        await message.answer("❌ Ошибка: ключ не найден.")
        await state.clear()
        return
    value = message.text.strip()
    if key in ('cert_x', 'cert_y', 'cert_font_size', 'cert_code_length', 
               'qr_image_x', 'qr_image_y', 'qr_image_size',
               'qr_pdf_x', 'qr_pdf_y', 'qr_pdf_size', 'qr_pdf_label_font_size'):
        try:
            int(value)
        except ValueError:
            await message.answer("❌ Введите целое число.")
            return
    if key == 'cert_font_color':
        try:
            parts = value.split(',')
            if len(parts) != 3:
                raise ValueError
            for p in parts:
                int(p.strip())
        except:
            await message.answer("❌ Введите цвет в формате R,G,B (например, 255,0,0)")
            return
    await set_setting(key, value)
    await message.answer(f"✅ Настройка '{key}' успешно изменена на '{value}'.")
    await state.clear()
    if key.startswith('qr_image_') or key.startswith('qr_pdf_'):
        await admin_qr_settings(message)
    else:
        await admin_cert_settings(message)

@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    await callback.message.answer("Выберите действие:", reply_markup=get_admin_keyboard())
    await callback.answer()

@dp.message(F.text == "🧪 Тестовый сертификат")
async def admin_test_certificate(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    templates = await get_all_templates()
    if not templates:
        await message.answer("❌ Нет загруженных шаблонов. Сначала добавьте шаблон через '🎨 Шаблоны'.")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for t in templates:
        keyboard.inline_keyboard.append(
            [InlineKeyboardButton(text=f"{t['amount']} ₽", callback_data=f"test_cert_{t['amount']}")]
        )
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="✏️ Ввести свою сумму", callback_data="test_cert_custom")])
    await message.answer(
        "Выберите номинал для тестового сертификата (будет отправлен вам):",
        reply_markup=keyboard
    )
    if state:
        await state.set_state(OrderStates.admin_waiting_test_amount)

@dp.callback_query(F.data.startswith("test_cert_"))
async def admin_test_cert_select(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    data = callback.data
    if data == "test_cert_custom":
        await callback.message.edit_text(
            "Введите сумму (в рублях) для тестового сертификата:\n"
            "Для отмены отправьте /cancel"
        )
        await state.set_state(OrderStates.admin_waiting_test_amount)
        await callback.answer()
        return
    amount = int(data.split("_")[2])
    await generate_and_send_test_certificate(callback.from_user.id, amount, callback.message)
    await state.clear()
    await callback.answer()

@dp.message(StateFilter(OrderStates.admin_waiting_test_amount))
async def admin_test_cert_custom_amount(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    try:
        amount = int(message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите положительное целое число.")
        return
    await generate_and_send_test_certificate(message.from_user.id, amount, message)
    await state.clear()

async def generate_and_send_test_certificate(admin_id: int, amount: int, target):
    try:
        cert_data = await create_gift_certificate(purchaser_id=admin_id, amount=amount)
        code = cert_data['code']
        image_path, pdf_path = await generate_certificate_files(code, amount)
        await bot.send_photo(admin_id, FSInputFile(image_path), caption="🧪 Тестовый сертификат (изображение)")
        if pdf_path and os.path.exists(pdf_path):
            await bot.send_document(admin_id, FSInputFile(pdf_path), caption="🧪 Тестовый сертификат (PDF)")
        gift_link = f"https://t.me/{BOT_USERNAME}?start=gift_{code}"
        text = (
            f"🧪 Тестовый сертификат\n\n"
            f"Сумма: {amount} ₽\n"
            f"Код: {code}\n\n"
            f"Ссылка для активации:\n{gift_link}\n\n"
            "Этот сертификат активен и может быть использован для заказа."
        )
        await bot.send_message(admin_id, text)
        if hasattr(target, 'edit_text'):
            await target.edit_text("✅ Тестовый сертификат создан и отправлен вам.")
        else:
            await target.answer("✅ Тестовый сертификат создан и отправлен вам.")
    except Exception as e:
        logger.error(f"Ошибка создания тестового сертификата: {e}")
        error_text = f"❌ Ошибка: {e}"
        if hasattr(target, 'edit_text'):
            await target.edit_text(error_text)
        else:
            await target.answer(error_text)

@dp.message(F.text == "📋 Сертификаты")
async def admin_list_certificates(message: types.Message):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🟢 Активные", callback_data="list_active")],
            [InlineKeyboardButton(text="🔴 Использованные", callback_data="list_used")],
            [InlineKeyboardButton(text="📋 Все", callback_data="list_all")]
        ]
    )
    await message.answer("Выберите группу сертификатов для просмотра:", reply_markup=keyboard)

@dp.callback_query(F.data.startswith("list_"))
async def list_certificates_by_status(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    status_filter = callback.data.split("_")[1]
    if status_filter == "all":
        query = '''
            SELECT c.*, u.first_name as purchaser_name, u.username as purchaser_username,
                   o.user_id as used_by_id, u2.first_name as used_by_name, u2.username as used_by_username
            FROM gift_certificates c
            LEFT JOIN users u ON c.purchaser_id = u.tg_id
            LEFT JOIN orders o ON c.order_id = o.id
            LEFT JOIN users u2 ON o.user_id = u2.tg_id
            ORDER BY c.created_at DESC
        '''
    elif status_filter == "active":
        query = '''
            SELECT c.*, u.first_name as purchaser_name, u.username as purchaser_username
            FROM gift_certificates c
            LEFT JOIN users u ON c.purchaser_id = u.tg_id
            WHERE c.status = 'active'
            ORDER BY c.created_at DESC
        '''
    elif status_filter == "used":
        query = '''
            SELECT c.*, u.first_name as purchaser_name, u.username as purchaser_username,
                   o.user_id as used_by_id, u2.first_name as used_by_name, u2.username as used_by_username,
                   o.id as order_id_used
            FROM gift_certificates c
            LEFT JOIN users u ON c.purchaser_id = u.tg_id
            LEFT JOIN orders o ON c.order_id = o.id
            LEFT JOIN users u2 ON o.user_id = u2.tg_id
            WHERE c.status = 'used'
            ORDER BY c.created_at DESC
        '''
    else:
        await callback.answer("Неизвестный фильтр", show_alert=True)
        return

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(query)

    if not rows:
        await callback.message.edit_text("📭 Сертификатов в этой группе нет.")
        await callback.answer()
        return

    text = f"📋 Сертификаты ({status_filter}):\n\n"
    for c in rows:
        status_emoji = {
            'active': '🟢',
            'used': '🔴',
            'expired': '⚪',
            'cancelled': '⚫'
        }.get(c['status'], '❓')
        text += f"{status_emoji} Код: {c['code']}\n"
        text += f"   Сумма: {c['amount']} ₽\n"
        text += f"   Покупатель: {c['purchaser_name'] or 'Неизвестно'} (@{c['purchaser_username'] or 'нет'})\n"
        if c['status'] == 'used':
            used_by = c.get('used_by_name') or 'Неизвестно'
            used_by_username = c.get('used_by_username') or 'нет'
            text += f"   Использовал: {used_by} (@{used_by_username})\n"
            if c['used_at']:
                used_str = (c['used_at'] + timedelta(hours=3)).strftime('%d.%m.%Y %H:%M') + " (МСК)"
            else:
                used_str = '—'
            text += f"   Дата использования: {used_str}\n"
        created_str = (c['created_at'] + timedelta(hours=3)).strftime('%d.%m.%Y %H:%M') + " (МСК)"
        text += f"   Дата покупки: {created_str}\n\n"

    while len(text) > 3500:
        break_pos = text.rfind('\n\n', 0, 3500)
        if break_pos == -1:
            break_pos = 3500
        part = text[:break_pos]
        await callback.message.answer(part)
        text = text[break_pos:].lstrip()
    await callback.message.answer(text)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Вернуться к выбору группы", callback_data="list_back")],
            [InlineKeyboardButton(text=f"🗑️ Удалить все ({status_filter})", callback_data=f"delete_all_{status_filter}")]
        ]
    )
    await callback.message.answer("Выберите действие:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "list_back")
async def list_back(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    await admin_list_certificates(callback.message)
    await callback.answer()

@dp.callback_query(F.data.startswith("delete_all_"))
async def delete_all_certificates_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    status = callback.data.split("_")[2]
    async with db_pool.acquire() as conn:
        if status == 'all':
            count = await conn.fetchval('SELECT COUNT(*) FROM gift_certificates')
        else:
            count = await conn.fetchval('SELECT COUNT(*) FROM gift_certificates WHERE LOWER(status) = LOWER($1)', status)
    if count == 0:
        await callback.answer("Нет сертификатов для удаления.", show_alert=True)
        return
    await state.update_data(delete_all_status=status)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, удалить все", callback_data="delete_all_confirm")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="delete_all_cancel")]
        ]
    )
    await callback.message.edit_text(
        f"Вы уверены, что хотите удалить все {count} сертификатов из группы '{status}'?\n"
        "Это действие необратимо.",
        reply_markup=keyboard
    )
    await callback.answer()

@dp.callback_query(F.data == "delete_all_confirm")
async def delete_all_confirm(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    data = await state.get_data()
    status = data.get('delete_all_status')
    if not status:
        await callback.answer("Ошибка: не определена группа.", show_alert=True)
        return

    async with db_pool.acquire() as conn:
        if status == 'all':
            rows = await conn.fetch('SELECT id, code FROM gift_certificates')
        else:
            rows = await conn.fetch('SELECT id, code FROM gift_certificates WHERE TRIM(LOWER(status)) = LOWER($1)', status)
        
        if not rows:
            await callback.answer("Нет сертификатов для удаления.", show_alert=True)
            return

        deleted = 0
        for row in rows:
            if await delete_gift_certificate_by_id(row['id']):
                deleted += 1

    await callback.message.edit_text(f"✅ Удалено {deleted} сертификатов.")
    await state.clear()
    await callback.answer()
    await admin_list_certificates(callback.message)

@dp.callback_query(F.data == "delete_all_cancel")
async def delete_all_cancel(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text("❌ Удаление отменено.")
    await callback.answer()
    await admin_list_certificates(callback.message)

@dp.message(F.text == "⚙️ Настройки PDF")
async def admin_pdf_settings(message: types.Message):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    enabled = await get_send_pdf_enabled()
    status = "✅ включена" if enabled else "❌ выключена"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Включить", callback_data="pdf_enable"),
             InlineKeyboardButton(text="Выключить", callback_data="pdf_disable")]
        ]
    )
    await message.answer(
        f"⚙️ Настройка отправки PDF-файлов сертификатов:\n\n"
        f"Текущий режим: {status}\n\n"
        f"Выберите действие:",
        reply_markup=keyboard
    )

@dp.callback_query(F.data == "pdf_enable")
async def pdf_enable(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    await set_send_pdf_enabled(True)
    await callback.message.edit_text("✅ Отправка PDF включена.")
    await callback.answer()

@dp.callback_query(F.data == "pdf_disable")
async def pdf_disable(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    await set_send_pdf_enabled(False)
    await callback.message.edit_text("❌ Отправка PDF выключена.")
    await callback.answer()

@dp.message(F.text == "🗑️ Удалить сертификат")
async def admin_delete_certificate_start(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    async with db_pool.acquire() as conn:
        certs = await conn.fetch('SELECT id, code, amount, status, created_at FROM gift_certificates ORDER BY created_at DESC LIMIT 50')
    if not certs:
        await message.answer("📭 Нет сертификатов для удаления.")
        return
    text = "🗑️ Выберите сертификат для удаления:\n(удаление необратимо)\n\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for c in certs:
        status_emoji = '🟢' if c['status'] == 'active' else '🔴'
        label = f"{status_emoji} {c['code']} ({c['amount']}₽)"
        keyboard.inline_keyboard.append(
            [InlineKeyboardButton(text=label, callback_data=f"del_cert_{c['id']}")]
        )
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_cert_back")])
    await message.answer(text, reply_markup=keyboard)

@dp.callback_query(F.data.startswith("del_cert_confirm_"))
async def admin_delete_cert_final(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    cert_id = int(callback.data.split("_")[3])
    success = await delete_gift_certificate_by_id(cert_id)
    if success:
        await callback.message.edit_text("✅ Сертификат успешно удалён.")
    else:
        await callback.message.edit_text("❌ Ошибка при удалении сертификата.")
    await state.clear()
    await callback.answer()

@dp.callback_query(F.data.startswith("del_cert_"))
async def admin_delete_cert_confirm(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    cert_id = int(callback.data.split("_")[2])
    cert = await get_gift_certificate_by_id(cert_id)
    if not cert:
        await callback.answer("Сертификат не найден", show_alert=True)
        return
    await state.update_data(del_cert_id=cert_id)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"del_cert_confirm_{cert_id}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cert_back")]
        ]
    )
    await callback.message.edit_text(
        f"Вы действительно хотите удалить сертификат?\n"
        f"Код: {cert['code']}\n"
        f"Сумма: {cert['amount']} ₽\n"
        f"Статус: {cert['status']}\n\n"
        "Это действие необратимо.",
        reply_markup=keyboard
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_cert_back")
async def admin_cert_back(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS or callback.from_user.id == bot.id:
        await callback.answer("Нет прав", show_alert=True)
        return
    await admin_certificates_menu(callback.message)
    await callback.answer()

@dp.message(F.text == "👤 Панель клиента")
async def admin_client_panel(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    await state.clear()
    await message.answer(
        "👤 Панель клиента.\n"
        "Вы можете протестировать работу бота от лица клиента.\n"
        "Для возврата в админ-панель введите /start.",
        reply_markup=get_main_keyboard()
    )

@dp.message(F.document)
async def handle_file_upload(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        await message.answer("❌ Нет прав.")
        return
    doc = message.document
    if not doc.file_name.endswith(('.pdf', '.doc', '.docx', '.txt')):
        await message.answer("❌ Поддерживаются только PDF, DOC, DOCX, TXT")
        return
    if doc.file_size > 50 * 1024 * 1024:
        await message.answer("❌ Файл слишком большой (макс 50 МБ)")
        return

    file_id = doc.file_id
    file_name = doc.file_name

    await state.update_data(temp_file_id=file_id, temp_file_name=file_name)
    await message.answer("✏️ Введите номер заказа (число), к которому прикрепить этот файл:")
    await state.set_state(OrderStates.waiting_upload_order_id)

@dp.message(StateFilter(OrderStates.waiting_upload_order_id))
async def process_upload_order_id(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    try:
        order_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите число (номер заказа)")
        return
    order = await get_order(order_id)
    if not order:
        await message.answer("❌ Заказ с таким номером не найден.")
        return
    if order['status'] not in ('paid', 'processing'):
        await message.answer(f"❌ Заказ №{order_id} не в статусе оплачен или в работе (текущий статус: {order['status']}).")
        return
    data = await state.get_data()
    file_id = data.get('temp_file_id')
    file_name = data.get('temp_file_name')
    if not file_id:
        await message.answer("❌ Ошибка: file_id не найден. Загрузите файл заново.")
        await state.clear()
        return

    await save_order_file(order_id, file_id, file_name)

    user = await get_user(order['user_id'])
    if user:
        await bot.send_document(
            user['tg_id'],
            file_id,
            caption=f"📄 Ваш разбор по заказу №{order_id} готов! Благодарим за доверие."
        )
        await bot.send_message(
            user['tg_id'],
            "Если хотите заказать ещё один или приобрести подарочный сертификат – воспользуйтесь меню ниже.",
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer("❌ Пользователь не найден")
        await state.clear()
        return

    await anonymize_order(order_id)
    await message.answer(f"✅ Файл отправлен по заказу №{order_id}.\n📁 File ID: {file_id}")
    await state.clear()

@dp.message(Command("anonymize"))
async def cmd_anonymize(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        await message.answer("⛔ Нет прав.")
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /anonymize <номер_заказа>")
        return
    try:
        order_id = int(parts[1])
    except ValueError:
        await message.answer("❌ Номер заказа должен быть числом.")
        return
    success = await anonymize_order(order_id)
    if success:
        await message.answer(f"✅ Заказ №{order_id} успешно анонимизирован.")
    else:
        await message.answer(f"❌ Не удалось анонимизировать заказ №{order_id}. Проверьте, что заказ существует и находится в статусе 'done'.")

@dp.message(Command("getfile"))
async def cmd_getfile(message: types.Message):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        await message.answer("⛔ Нет прав.")
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /getfile <номер_заказа>")
        return
    try:
        order_id = int(parts[1])
    except ValueError:
        await message.answer("❌ Номер заказа должен быть числом.")
        return
    order = await get_order(order_id)
    if not order:
        await message.answer(f"❌ Заказ №{order_id} не найден.")
        return
    if not order['file_id']:
        await message.answer(f"❌ Файл для заказа №{order_id} отсутствует.")
        return
    try:
        await bot.send_document(
            message.chat.id,
            order['file_id'],
            caption=f"📁 Файл для заказа №{order_id}"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка отправки файла: {e}")

@dp.message(Command("resetdb"))
async def cmd_resetdb(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        await message.answer("⛔ Нет прав.")
        return
    await message.answer(
        "⚠️ ВНИМАНИЕ! Вы собираетесь полностью удалить всю базу данных.\n"
        "Это действие НЕОБРАТИМО! Все заказы, пользователи, платежи и настройки будут удалены.\n\n"
        "Для подтверждения введите YES (заглавными буквами).\n"
        "Для отмены отправьте /cancel или любое другое сообщение."
    )
    await state.set_state(AdminStates.waiting_reset_confirm1)

@dp.message(StateFilter(AdminStates.waiting_reset_confirm1))
async def process_reset_confirm1(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    if message.text.strip() != "YES":
        await message.answer("❌ Сброс отменён (неверный код).")
        await state.clear()
        return
    await message.answer(
        "⚠️ ПОСЛЕДНЕЕ ПРЕДУПРЕЖДЕНИЕ!\n"
        "Вы уверены, что хотите безвозвратно удалить ВСЕ данные?\n\n"
        "Для окончательного подтверждения введите CONFIRM (заглавными буквами).\n"
        "Для отмены отправьте /cancel или любое другое сообщение."
    )
    await state.set_state(AdminStates.waiting_reset_confirm2)

@dp.message(StateFilter(AdminStates.waiting_reset_confirm2))
async def process_reset_confirm2(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    if message.text.strip() != "CONFIRM":
        await message.answer("❌ Сброс отменён (неверный код).")
        await state.clear()
        return
    await message.answer("⏳ Начинаю сброс базы данных...")
    try:
        await drop_tables()
        await create_tables()
        await message.answer("✅ База данных полностью пересоздана. Все данные удалены.")
        logger.warning(f"База данных сброшена администратором {message.from_user.id}")
    except Exception as e:
        logger.error(f"Ошибка сброса БД: {e}")
        await message.answer(f"❌ Ошибка при сбросе: {e}")
    finally:
        await state.clear()

@dp.message(F.text == "📢 Рассылка")
async def admin_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    await message.answer(
        "📢 Отправьте сообщение для рассылки.\n\n"
        "Поддерживаются:\n"
        "• Текст (с форматированием Markdown)\n"
        "• Фото (с подписью)\n"
        "• Видео (с подписью)\n"
        "• Документы (с подписью)\n"
        "• Голосовые, анимации и любые другие типы\n\n"
        "Для отмены отправьте /cancel"
    )
    await state.set_state(OrderStates.waiting_broadcast_content)

@dp.message(StateFilter(OrderStates.waiting_broadcast_content))
async def process_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Рассылка отменена.")
        return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch('SELECT tg_id FROM users')
    if not rows:
        await message.answer("❌ Нет пользователей для рассылки.")
        await state.clear()
        return
    await message.answer(f"⏳ Начинаю рассылку {len(rows)} пользователям...")
    sent = 0
    for row in rows:
        try:
            await bot.copy_message(
                chat_id=row['tg_id'],
                from_chat_id=message.chat.id,
                message_id=message.message_id
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Ошибка отправки пользователю {row['tg_id']}: {e}")
    await message.answer(f"✅ Рассылка завершена. Отправлено: {sent} сообщений.")
    await state.clear()

@dp.message(F.text == "💰 Изменить цену")
async def admin_change_price_old(message: types.Message, state: FSMContext):
    # Этот обработчик оставлен для обратной совместимости, но теперь цена меняется через настройки платежей
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    await message.answer(
        "💰 Эта функция перенесена в раздел 'Настройки платежей'.\n"
        "Используйте кнопку 'Настройки платежей' в админ-панели."
    )

@dp.message(F.text == "🔍 Проверить платежи")
async def admin_check_payment_system_old(message: types.Message):
    # Этот обработчик оставлен для обратной совместимости, но теперь проверка платежей в настройках
    if message.from_user.id not in ADMIN_IDS or message.from_user.id == bot.id:
        return
    await message.answer(
        "🔍 Эта функция перенесена в раздел 'Настройки платежей'.\n"
        "Используйте кнопку 'Настройки платежей' в админ-панели."
    )

async def main():
    os.makedirs('media/templates', exist_ok=True)
    os.makedirs('media/certificates', exist_ok=True)
    os.makedirs('fonts', exist_ok=True)
    await init_db_pool()
    
    # Удаляем webhook, чтобы использовать polling (избегаем конфликта)
    await bot.delete_webhook(drop_pending_updates=True)
    
    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        await close_db_pool()

if __name__ == "__main__":
    asyncio.run(main())
