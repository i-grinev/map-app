import requests
import json
import re
import hashlib
import time
import os
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# КОНФИГУРАЦИЯ (из секретов GitHub)
# ============================================================
BITRIX_WEBHOOK = os.environ.get('BITRIX_WEBHOOK')
ENTITY_TYPE_ID = 1038
ADDRESS_FIELD = 'ufCrm8FullAdress'
YANDEX_API_KEY = os.environ.get('YANDEX_API_KEY', '')

# Настройки скорости
MAX_WORKERS = 10
BATCH_SIZE = 50
CACHE_FILE = 'data/geocode_cache.json'

# Стадии, которые ИГНОРИРУЕМ (не геокодируем и не выводим)
IGNORE_STAGES = ['UC_QA1YNG']

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
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

# ============================================================
# ФУНКЦИЯ ДЛЯ ИЗВЛЕЧЕНИЯ СТАДИИ ИЗ ПОЛНОГО ID
# ============================================================
def get_stage_short(stage_id):
    if not stage_id:
        return 'default'
    parts = stage_id.split(':')
    return parts[-1] if parts else 'default'

# ============================================================
# ПРОВЕРКА — НУЖНО ЛИ ИГНОРИРОВАТЬ
# ============================================================
def should_ignore(stage_id):
    """Проверяет, нужно ли игнорировать объект по стадии"""
    if not stage_id:
        return False
    stage = get_stage_short(stage_id)
    return stage in IGNORE_STAGES

# ============================================================
# НОРМАЛИЗАЦИЯ АДРЕСА (исправленная версия)
# ============================================================
def normalize_address(address):
    if not address:
        return ''
    
    text = str(address).strip()
    
    # Удаляем звездочки и решетки
    text = text.replace('*', '').replace('#', '')
    
    # Заменяем переносы строк на пробелы
    text = text.replace('\n', ' ').replace('\r', ' ')
    
    # Удаляем лишние комментарии в скобках, но сохраняем важную информацию
    # Например: (МЦК Стрешнево) - это важно для геокодирования
    text = re.sub(r'\([^)]*МЦ[^)]*\)', '', text)  # Удаляем только если есть МЦК/МЦД
    text = re.sub(r'\([^)]*метро[^)]*\)', '', text, flags=re.IGNORECASE)
    
    # Удаляем только явные лишние части
    text = re.sub(r',?\s*код домофона[\s\S]*?(?=,|$)', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*домофон[\s\S]*?(?=,|$)', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*ключ[\s\S]*?(?=,|$)', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*парковка[\s\S]*?(?=,|$)', '', text, flags=re.IGNORECASE)
    
    # Преобразуем сокращения (сохраняя структуру адреса)
    replacements = {
        r'\bул\.\b': 'улица',
        r'\bпр-д\b': 'проезд',
        r'\bпр-кт\b': 'проспект',
        r'\bпр-т\b': 'проспект',
        r'\bпр\.\b': 'проспект',  # Важно: проспект, а не проезд!
        r'\bпер\.\b': 'переулок',
        r'\bш\.\b': 'шоссе',
        r'\bнаб\.\b': 'набережная',
        r'\bб-р\b': 'бульвар',
        r'\bбул\.\b': 'бульвар',
        r'\bпос\.\b': 'поселок',
        r'\bд\.\b': 'дом',
        r'\bк\.\b': 'корпус',
        r'\bстр\.\b': 'строение',
        r'\bкорп\.\b': 'корпус',
        r'\bг\.\b': 'город',
    }
    
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    
    # Обработка форматов типа "д. 23к7" или "23к7"
    text = re.sub(r'(\d+)к(\d+)', r'\1 корпус \2', text, flags=re.IGNORECASE)
    text = re.sub(r'дом\s*(\d+)\s*к(\d+)', r'дом \1 корпус \2', text, flags=re.IGNORECASE)
    
    # Удаляем только ОЧЕВИДНО лишние детали (квартиры, этажи, подъезды)
    # НО сохраняем номер дома, корпус, строение
    patterns_to_remove = [
        (r',?\s*кв\.\s*[\d/А-Яа-яёЁ]+', ''),  # квартира
        (r',?\s*квартира\s*[\d/А-Яа-яёЁ]+', ''),
        (r',?\s*апарт\.?\s*[\d/А-Яа-яёЁ]+', ''),
        (r',?\s*апартаменты\s*[\d/А-Яа-яёЁ]+', ''),
        (r',?\s*пом\.\s*[\d/А-Яа-яёЁ]+', ''),
        (r',?\s*помещение\s*[\d/А-Яа-яёЁ]+', ''),
        (r',?\s*студия\s*[\d/А-Яа-яёЁ]+', ''),
        (r',?\s*ком\.\s*[\d/А-Яа-яёЁ]+', ''),
        (r',?\s*комната\s*[\d/А-Яа-яёЁ]+', ''),
        (r',?\s*этаж\s*\d+[А-Яа-яёЁ]?', ''),
        (r',?\s*эт\.?\s*\d+[А-Яа-яёЁ]?', ''),
        (r',?\s*эт\s*\d+[А-Яа-яёЁ]?', ''),
        (r',?\s*подъезд\s*\d+[А-Яа-яёЁ]?', ''),
        (r',?\s*под\.?\s*\d+[А-Яа-яёЁ]?', ''),
        (r',?\s*п\.\s*\d+[А-Яа-яёЁ]?', ''),
        (r',?\s*секция\s*\d+', ''),
        (r',?\s*парадная\s*\d+', ''),
        (r',?\s*на первом уровне секции', ''),
        (r',?\s*на втором уровне секции', ''),
    ]
    
    for pattern, replacement in patterns_to_remove:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    
    # Убираем лишние запятые и пробелы
    text = re.sub(r',\s*,', ',', text)
    text = re.sub(r'\s+', ' ', text)
    text = text.strip().rstrip(',').rstrip('.')
    
    # Если адрес слишком короткий, пытаемся восстановить его
    if len(text) < 5:
        return text
    
    return text

# ============================================================
# ГЕОКОДИРОВАНИЕ
# ============================================================
def geocode_address(address, cache):
    if not address:
        return None
    
    cache_key = hashlib.md5(address.encode()).hexdigest()
    
    if cache_key in cache and cache[cache_key]:
        return cache[cache_key]
    
    coords = None
    
    if YANDEX_API_KEY:
        coords = geocode_yandex(address)
        if coords:
            cache[cache_key] = coords
            save_cache(cache)
            return coords
    
    coords = geocode_osm(address)
    if coords:
        cache[cache_key] = coords
        save_cache(cache)
        return coords
    
    # Если не нашли по полному адресу, пробуем упрощенный вариант
    simplified = simplify_address(address)
    if simplified and simplified != address:
        coords = geocode_yandex(simplified) if YANDEX_API_KEY else geocode_osm(simplified)
        if coords:
            cache[cache_key] = coords
            save_cache(cache)
            return coords
    
    cache[cache_key] = None
    save_cache(cache)
    return None

def simplify_address(address):
    """Упрощает адрес для поиска (убирает корпуса если не найдено)"""
    # Убираем корпус если есть
    simplified = re.sub(r',?\s*корпус\s*\d+', '', address, flags=re.IGNORECASE)
    simplified = re.sub(r',?\s*строение\s*\d+', '', simplified, flags=re.IGNORECASE)
    return simplified.strip()

def geocode_yandex(address):
    if not YANDEX_API_KEY:
        return None
    
    try:
        url = "https://geocode-maps.yandex.ru/1.x/"
        params = {
            'apikey': YANDEX_API_KEY,
            'geocode': address,
            'format': 'json',
            'results': 1,
            'lang': 'ru_RU'
        }
        response = requests.get(url, params=params, timeout=5)
        
        if response.status_code == 200:
            data = response.json()
            members = data.get('response', {}).get('GeoObjectCollection', {}).get('featureMember', [])
            if members:
                pos = members[0]['GeoObject']['Point']['pos']
                lon, lat = pos.split(' ')
                lat, lon = float(lat), float(lon)
                return {'lat': lat, 'lon': lon}
        return None
    except Exception as e:
        print(f"   Yandex error: {e}")
        return None

def geocode_osm(address):
    try:
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            'q': address,
            'format': 'json',
            'limit': 1,
            'accept-language': 'ru'
        }
        response = requests.get(
            url,
            params=params,
            headers={'User-Agent': 'MapApp/1.0 (https://i-grinev.github.io/map-app)'},
            timeout=5
        )
        
        if response.status_code == 200 and response.json():
            data = response.json()[0]
            lat, lon = float(data['lat']), float(data['lon'])
            return {'lat': lat, 'lon': lon}
        return None
    except Exception as e:
        print(f"   OSM error: {e}")
        return None

# ============================================================
# ПАРАЛЛЕЛЬНАЯ ОБРАБОТКА (с игнорированием)
# ============================================================
def process_item(item, cache):
    stage_id = item.get('stageId', '')
    
    # Проверяем — нужно ли игнорировать
    if should_ignore(stage_id):
        return {
            'id': item.get('id'),
            'title': item.get('title', ''),
            'address': item.get(ADDRESS_FIELD, ''),
            'address_clean': '',
            'lat': None,
            'lon': None,
            'stage_id': stage_id,
            'stage_name': item.get('stage_name', ''),
            'ignored': True
        }, False
    
    address = item.get(ADDRESS_FIELD, '')
    clean = normalize_address(address) if address else ''
    
    # Если адрес очищен слишком сильно, пробуем использовать оригинал
    if clean and len(clean) < 10:
        clean = address
    
    coords = geocode_address(clean, cache) if clean else None
    
    return {
        'id': item.get('id'),
        'title': item.get('title', ''),
        'address': address,
        'address_clean': clean,
        'lat': coords['lat'] if coords else None,
        'lon': coords['lon'] if coords else None,
        'stage_id': stage_id,
        'stage_name': item.get('stage_name', ''),
        'ignored': False
    }, coords is not None

def process_parallel(items, cache):
    results = []
    geocoded = 0
    ignored = 0
    total = len(items)
    start_time = time.time()
    
    print(f"   Запуск {MAX_WORKERS} параллельных потоков...")
    print(f"   Игнорируем стадию: {IGNORE_STAGES}")
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(process_item, item, cache): i 
            for i, item in enumerate(items)
        }
        
        completed = 0
        for future in as_completed(futures):
            try:
                result, found = future.result(timeout=30)
                results.append(result)
                if found:
                    geocoded += 1
                if result.get('ignored'):
                    ignored += 1
                
                completed += 1
                if completed % BATCH_SIZE == 0 or completed == total:
                    elapsed = time.time() - start_time
                    print(f"   Обработано: {completed}/{total} | Найдено: {geocoded} | Игнорировано: {ignored} | Время: {elapsed:.0f}с")
                    
            except Exception as e:
                print(f"   ❌ Ошибка: {e}")
                completed += 1
    
    return results, geocoded, ignored

# ============================================================
# ЗАПРОС К БИТРИКС24
# ============================================================
def fetch_from_bitrix():
    all_items = []
    start = 0
    limit = 50
    
    print(f"📥 Загрузка из Битрикс24...")
    
    while True:
        params = {
            "entityTypeId": ENTITY_TYPE_ID,
            "select": ['id', 'title', ADDRESS_FIELD, 'stageId', 'stage_name'],
            "order": {"id": "asc"},
            "start": start,
            "limit": limit
        }
        
        try:
            response = requests.post(f"{BITRIX_WEBHOOK}crm.item.list", json=params, timeout=30)
            data = response.json()
            
            if 'error' in data:
                raise Exception(f"Bitrix error: {data.get('error_description')}")
            
            items = data.get('result', {}).get('items', [])
            if not items:
                break
            
            all_items.extend(items)
            print(f"   Загружено: {len(all_items)} записей")
            
            if len(items) < limit:
                break
            start += limit
            
        except Exception as e:
            print(f"   ❌ Ошибка: {e}")
            break
    
    return all_items

# ============================================================
# ОСНОВНАЯ ФУНКЦИЯ
# ============================================================
def main():
    print(f"🔄 Обновление данных: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   Яндекс ключ: {'✅ Есть' if YANDEX_API_KEY else '❌ Нет'}")
    print(f"   Игнорируем стадии: {IGNORE_STAGES}")
    
    os.makedirs('data', exist_ok=True)
    cache = load_cache()
    print(f"   Кэш: {len(cache)} записей")
    
    items = fetch_from_bitrix()
    if not items:
        print("❌ Нет данных из Битрикс24")
        return
    
    # Фильтруем игнорируемые ДО геокодирования (для скорости)
    total_before = len(items)
    items_to_process = [item for item in items if not should_ignore(item.get('stageId', ''))]
    ignored_count = total_before - len(items_to_process)
    print(f"   Всего: {total_before}, игнорируем: {ignored_count}, обрабатываем: {len(items_to_process)}")
    
    print(f"📍 Геокодирование...")
    results, geocoded, ignored = process_parallel(items_to_process, cache)
    
    # Добавляем игнорируемые объекты в результат
    ignored_results = []
    for item in items:
        if should_ignore(item.get('stageId', '')):
            ignored_results.append({
                'id': item.get('id'),
                'title': item.get('title', ''),
                'address': item.get(ADDRESS_FIELD, ''),
                'address_clean': '',
                'lat': None,
                'lon': None,
                'stage_id': item.get('stageId', ''),
                'stage_name': item.get('stage_name', ''),
                'ignored': True
            })
    
    all_results = results + ignored_results
    
    save_cache(cache)
    
    output_file = 'data/addresses.json'
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump({
            'updated_at': datetime.now().isoformat(),
            'total': len(all_results),
            'geocoded': geocoded,
            'ignored': ignored_count,
            'items': all_results
        }, f, ensure_ascii=False, indent=2)
    
    total = len(all_results)
    print(f"\n✅ Готово! Всего: {total}, с координатами: {geocoded}, игнорировано: {ignored_count}")
    print(f"   Файл: {output_file}")

if __name__ == '__main__':
    main()
