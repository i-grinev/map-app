import requests
import json
import re
import hashlib
import time
import os
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================
BITRIX_WEBHOOK = os.environ.get('BITRIX_WEBHOOK')
ENTITY_TYPE_ID = 1038
ADDRESS_FIELD = 'ufCrm8FullAdress'
YANDEX_API_KEY = os.environ.get('YANDEX_API_KEY', '')

MAX_WORKERS = 10
BATCH_SIZE = 50
CACHE_FILE = 'data/geocode_cache.json'
CACHE_TTL = 604800  # 7 дней

if not BITRIX_WEBHOOK:
    raise Exception("❌ BITRIX_WEBHOOK не задан в переменных окружения!")

# ============================================================
# КЭШ
# ============================================================
def load_cache():
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}

def save_cache(cache):
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

def is_cache_valid(cached_entry, current_address):
    """Проверяет, актуален ли кэш для данного адреса"""
    if not cached_entry:
        return False
    
    # Проверяем, совпадает ли адрес
    if cached_entry.get('address') != current_address:
        return False
    
    # Проверяем, не устарел ли кэш
    timestamp = cached_entry.get('timestamp', 0)
    if time.time() - timestamp > CACHE_TTL:
        return False
    
    return True

# ============================================================
# ГЕОКОДИРОВАНИЕ С КЭШЕМ ПО ID
# ============================================================
def geocode_address_with_cache(item, cache):
    item_id = str(item.get('id'))
    address = item.get(ADDRESS_FIELD, '')
    
    if not address:
        return None, ''
    
    # Нормализуем адрес
    clean = normalize_address(address)
    if not clean:
        return None, ''
    
    # Ключ кэша по ID записи
    cache_key = f"item_{item_id}"
    
    # Проверяем кэш
    if cache_key in cache:
        cached = cache[cache_key]
        if is_cache_valid(cached, address):
            coords = cached.get('coords')
            if coords:
                return coords, cached.get('address_clean', clean)
    
    # Геокодируем
    coords = geocode_yandex(clean)
    if not coords:
        coords = geocode_osm(clean)
    
    # Сохраняем в кэш
    cache[cache_key] = {
        'address': address,
        'address_clean': clean,
        'coords': coords,
        'timestamp': time.time()
    }
    save_cache(cache)
    
    return coords, clean

# ============================================================
# ОСТАЛЬНЫЕ ФУНКЦИИ (без изменений)
# ============================================================
# ... (geocode_yandex, geocode_osm, normalize_address и т.д.)

def process_item(item, cache):
    item_id = item.get('id')
    address = item.get(ADDRESS_FIELD, '')
    
    coords, clean = geocode_address_with_cache(item, cache)
    
    return {
        'id': item_id,
        'title': item.get('title', ''),
        'address': address,
        'address_clean': clean,
        'lat': coords['lat'] if coords else None,
        'lon': coords['lon'] if coords else None,
        'stage_id': item.get('stageId', ''),
        'stage_name': item.get('stage_name', ''),
        'ignored': False
    }, coords is not None

# Остальной код без изменений...
