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
MAX_WORKERS = 10          # Количество параллельных запросов
BATCH_SIZE = 50           # Размер пакета для вывода прогресса
CACHE_FILE = 'data/geocode_cache.json'

# Проверяем наличие ключей
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

# ============================================================
# НОРМАЛИЗАЦИЯ АДРЕСА (минимальная)
# ============================================================
def normalize_address(address):
    if not address:
        return ''
    text = str(address).strip()
    text = text.replace('*', '').replace('#', '')
    text = text.replace('\n', ' ').replace('\r', ' ')
    text = re.sub(r'\s+', ' ', text)
    text = text.strip().rstrip(',').rstrip('.')
    return text if len(text) >= 5 else ''

# ============================================================
# ГЕОКОДИРОВАНИЕ (быстрое)
# ============================================================
def geocode_yandex(address):
    """Геокодирование через Яндекс (быстрый режим)"""
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
        # Уменьшаем таймаут до 3 секунд
        response = requests.get(url, params=params, timeout=3)
        
        if response.status_code == 200:
            data = response.json()
            members = data.get('response', {}).get('GeoObjectCollection', {}).get('featureMember', [])
            if members:
                pos = members[0]['GeoObject']['Point']['pos']
                lon, lat = pos.split(' ')
                return {'lat': float(lat), 'lon': float(lon)}
        return None
    except:
        return None

def geocode_osm(address):
    """Геокодирование через OpenStreetMap (быстрый режим)"""
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
            headers={'User-Agent': 'MapApp/1.0'},
            timeout=3  # Уменьшаем таймаут
        )
        
        if response.status_code == 200 and response.json():
            data = response.json()[0]
            return {'lat': float(data['lat']), 'lon': float(data['lon'])}
        return None
    except:
        return None

def geocode_address(address, cache):
    """Геокодирование с кэшем"""
    if not address:
        return None
    
    cache_key = hashlib.md5(address.encode()).hexdigest()
    
    # Проверяем кэш
    if cache_key in cache and cache[cache_key]:
        return cache[cache_key]
    
    # Сначала пробуем Яндекс (если есть ключ) — он быстрее
    if YANDEX_API_KEY:
        coords = geocode_yandex(address)
        if coords and (abs(coords['lat'] - 55.7558) > 0.01 or abs(coords['lon'] - 37.6173) > 0.01):
            cache[cache_key] = coords
            return coords
    
    # Потом OSM
    coords = geocode_osm(address)
    if coords and (abs(coords['lat'] - 55.7558) > 0.01 or abs(coords['lon'] - 37.6173) > 0.01):
        cache[cache_key] = coords
        return coords
    
    # Не найдено
    cache[cache_key] = None
    return None

# ============================================================
# ПАРАЛЛЕЛЬНАЯ ОБРАБОТКА
# ============================================================
def process_item(item, cache):
    """Обработка одного элемента"""
    address = item.get(ADDRESS_FIELD, '')
    clean = normalize_address(address) if address else ''
    
    coords = None
    if clean:
        coords = geocode_address(clean, cache)
    
    return {
        'id': item.get('id'),
        'title': item.get('title', ''),
        'address': address,
        'address_clean': clean,
        'lat': coords['lat'] if coords else None,
        'lon': coords['lon'] if coords else None,
        'stage_id': item.get('stageId', ''),
        'stage_name': item.get('stage_name', '')
    }, coords is not None

def process_addresses_parallel(items, cache):
    """Параллельная обработка всех адресов"""
    results = []
    geocoded = 0
    total = len(items)
    start_time = time.time()
    
    print(f"   Запуск {MAX_WORKERS} параллельных потоков...")
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Отправляем все задачи
        futures = {
            executor.submit(process_item, item, cache): i 
            for i, item in enumerate(items)
        }
        
        # Обрабатываем результаты по мере завершения
        completed = 0
        for future in as_completed(futures):
            try:
                result, found = future.result(timeout=30)
                results.append(result)
                if found:
                    geocoded += 1
                
                completed += 1
                if completed % BATCH_SIZE == 0 or completed == total:
                    elapsed = time.time() - start_time
                    print(f"   Обработано: {completed}/{total} | Найдено: {geocoded} | Время: {elapsed:.0f}с")
                    
            except Exception as e:
                print(f"   ❌ Ошибка: {e}")
                completed += 1
    
    return results, geocoded

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
    
    # Загружаем кэш
    cache = load_cache()
    print(f"   Кэш: {len(cache)} записей")
    
    # Получаем данные из Битрикса
    items = fetch_from_bitrix()
    if not items:
        print("❌ Нет данных из Битрикс24")
        return
    
    # Параллельное геокодирование
    print(f"📍 Геокодирование ({len(items)} адресов)...")
    results, geocoded = process_addresses_parallel(items, cache)
    
    # Сохраняем кэш
    save_cache(cache)
    
    # Сохраняем результаты
    output_file = 'data/addresses.json'
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump({
            'updated_at': datetime.now().isoformat(),
            'total': len(results),
            'geocoded': geocoded,
            'items': results
        }, f, ensure_ascii=False, indent=2)
    
    total = len(results)
    print(f"\n✅ Готово! Всего: {total}, с координатами: {geocoded} ({geocoded/total*100:.0f}%)")
    print(f"   Файл: {output_file}")

if __name__ == '__main__':
    main()
