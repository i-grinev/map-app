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

MAX_WORKERS = 10
BATCH_SIZE = 50
CACHE_FILE = 'data/geocode_cache.json'

# Игнорируемые стадии (не геокодируются и не выводятся)
IGNORE_STAGES = ['UC_QA1YNG']

if not BITRIX_WEBHOOK:
    raise Exception("❌ BITRIX_WEBHOOK не задан в переменных окружения!")

# ============================================================
# КЭШ (с очисткой при каждом запуске)
# ============================================================
def clear_cache():
    """Полностью очищает кэш при каждом запуске"""
    try:
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)
            print(f"   🗑️ Кэш очищен: {CACHE_FILE}")
        else:
            print(f"   ℹ️ Кэш-файл не найден, создаём новый")
    except Exception as e:
        print(f"   ⚠️ Ошибка при очистке кэша: {e}")

def load_cache():
    """Загружает кэш (после очистки он будет пустым)"""
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}

def save_cache(cache):
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================
def get_stage_short(stage_id):
    if not stage_id:
        return 'default'
    parts = stage_id.split(':')
    return parts[-1] if parts else 'default'

def should_ignore(stage_id):
    if not stage_id:
        return False
    return get_stage_short(stage_id) in IGNORE_STAGES

# ============================================================
# НОРМАЛИЗАЦИЯ АДРЕСА (ФИНАЛЬНАЯ ВЕРСИЯ)
# ============================================================
def normalize_address(address):
    """
    Очищает адрес, но СОХРАНЯЕТ все важные части:
    - город, улицу, дом, корпус, строение
    - Убирает только явный мусор
    """
    if not address:
        return ''
    
    text = str(address).strip()
    
    # 1. Убираем звёздочки и решётки
    text = text.replace('*', '').replace('#', '')
    text = text.replace('\n', ' ').replace('\r', ' ')
    
    # 2. Убираем скобки
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'\[[^\]]*\]', '', text)
    
    # 3. Убираем "Адрес:" в начале
    text = re.sub(r'^Адрес\s*:?\s*', '', text, flags=re.IGNORECASE)
    
    # 4. Убираем "г." в начале (НО сохраняем название города)
    text = re.sub(r'^\s*г\.\s*', '', text, flags=re.IGNORECASE)
    
    # 5. Убираем ЖК, МЦД, МЦК, Метро (только если они отдельно)
    text = re.sub(r'\b(ЖК|МЦД|МЦК|Метро)\s*[«"][^»"]*[»"]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\b(ЖК|МЦД|МЦК|Метро)\s+[А-Яа-яёЁA-Za-z]+\s*', '', text, flags=re.IGNORECASE)
    
    # 6. Заменяем сокращения на полные слова (ПОЛНЫЙ СПИСОК)
    replacements = {
        r'\bул\.\b': 'улица',
        r'\bпр-д\b': 'проезд',
        r'\bпр-кт\b': 'проспект',
        r'\bпр-т\b': 'проспект',
        r'\bпр\.\b': 'проезд',
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
        r'\bкор\b': 'корпус',        # "кор" без точки
        r'\bстр\b': 'строение',      # "стр" без точки
    }
    
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    
    # 7. Формат "23к7" -> "23 корпус 7"
    text = re.sub(r'(\d+)к(\d+)', r'\1 корпус \2', text, flags=re.IGNORECASE)
    
    # 8. Убираем ТОЛЬКО явные пояснения в КОНЦЕ
    text = re.sub(r',?\s*код\s*домофона[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*домофон[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*ключ[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*парковка[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*Wi-Fi[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*Важно[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*обязательно[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*геолокация[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*со стороны[\s\S]*$', '', text, flags=re.IGNORECASE)
    
    # 9. Чистим лишние пробелы и запятые
    text = re.sub(r',+', ',', text)
    text = re.sub(r'\s+', ' ', text)
    text = text.strip().rstrip(',').rstrip('.')
    
    if len(text) < 5:
        return text
    
    # 10. Добавляем "Москва" только если нет города
    cities = r'(Москва|Санкт-Петербург|Краснодар|Ялта|Сочи|Казань|Екатеринбург|Новосибирск|Мытищи|Видное|Люберцы|Химки|Долгопрудный|Ступино|Котельники|Красногорск|область|край|республика|район|поселок|деревня|село|город)'
    if not re.search(cities, text, re.IGNORECASE):
        text = 'Москва, ' + text
    
    return text

# ============================================================
# ГЕОКОДИРОВАНИЕ
# ============================================================
def geocode_yandex(address):
    if not YANDEX_API_KEY:
        return None
    try:
        url = "https://geocode-maps.yandex.ru/1.x/"
        params = {'apikey': YANDEX_API_KEY, 'geocode': address, 'format': 'json', 'results': 1, 'lang': 'ru_RU'}
        response = requests.get(url, params=params, timeout=5)
        if response.status_code == 200:
            data = response.json()
            members = data.get('response', {}).get('GeoObjectCollection', {}).get('featureMember', [])
            if members:
                pos = members[0]['GeoObject']['Point']['pos']
                lon, lat = pos.split(' ')
                lat, lon = float(lat), float(lon)
                if abs(lat - 55.7558) > 0.01 or abs(lon - 37.6173) > 0.01:
                    return {'lat': lat, 'lon': lon}
        return None
    except:
        return None

def geocode_osm(address):
    try:
        url = "https://nominatim.openstreetmap.org/search"
        params = {'q': address, 'format': 'json', 'limit': 1, 'accept-language': 'ru'}
        response = requests.get(url, params=params, headers={'User-Agent': 'MapApp/1.0'}, timeout=5)
        if response.status_code == 200 and response.json():
            data = response.json()[0]
            lat, lon = float(data['lat']), float(data['lon'])
            if abs(lat - 55.7558) > 0.01 or abs(lon - 37.6173) > 0.01:
                return {'lat': lat, 'lon': lon}
        return None
    except:
        return None

def geocode_address(address, cache):
    if not address:
        return None
    cache_key = hashlib.md5(address.encode()).hexdigest()
    
    # Проверяем кэш
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
    
    cache[cache_key] = None
    save_cache(cache)
    return None

# ============================================================
# ПАРАЛЛЕЛЬНАЯ ОБРАБОТКА
# ============================================================
def process_item(item, cache):
    stage_id = item.get('stageId', '')
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
    results, geocoded, ignored = [], 0, 0
    total, start_time = len(items), time.time()
    print(f"   Запуск {MAX_WORKERS} потоков...")
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_item, item, cache): i for i, item in enumerate(items)}
        completed = 0
        for future in as_completed(futures):
            try:
                result, found = future.result(timeout=30)
                results.append(result)
                if found: geocoded += 1
                if result.get('ignored'): ignored += 1
                completed += 1
                if completed % BATCH_SIZE == 0 or completed == total:
                    print(f"   {completed}/{total} | Найдено: {geocoded} | Игнор: {ignored} | {time.time()-start_time:.0f}с")
            except Exception as e:
                print(f"   ❌ Ошибка: {e}")
                completed += 1
    return results, geocoded, ignored

# ============================================================
# ЗАПРОС К БИТРИКС24
# ============================================================
def fetch_from_bitrix():
    all_items, start, limit = [], 0, 50
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
            if not items: break
            all_items.extend(items)
            print(f"   Загружено: {len(all_items)}")
            if len(items) < limit: break
            start += limit
        except Exception as e:
            print(f"   ❌ Ошибка: {e}")
            break
    return all_items

# ============================================================
# ОСНОВНАЯ ФУНКЦИЯ
# ============================================================
def main():
    print(f"🔄 Обновление: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   Яндекс ключ: {'✅' if YANDEX_API_KEY else '❌'}")
    
    # ============================================================
    # ВАЖНО: ОЧИЩАЕМ КЭШ ПРИ КАЖДОМ ЗАПУСКЕ
    # ============================================================
    clear_cache()
    
    # Загружаем пустой кэш
    cache = load_cache()
    print(f"   Кэш: {len(cache)} записей (очищен)")
    
    items = fetch_from_bitrix()
    if not items:
        print("❌ Нет данных")
        return
    
    total_before = len(items)
    items_to_process = [i for i in items if not should_ignore(i.get('stageId', ''))]
    ignored_count = total_before - len(items_to_process)
    print(f"   Всего: {total_before}, игнор: {ignored_count}, обраб: {len(items_to_process)}")
    
    print(f"📍 Геокодирование...")
    results, geocoded, ignored = process_parallel(items_to_process, cache)
    
    # Добавляем игнорируемые
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
    
    print(f"\n✅ Готово! Всего: {len(all_results)}, с координатами: {geocoded}, игнор: {ignored_count}")
    print(f"   Файл: {output_file}")

if __name__ == '__main__':
    main()
