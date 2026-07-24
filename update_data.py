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
CACHE_TTL = 604800  # 7 дней (в секундах)

if not BITRIX_WEBHOOK:
    raise Exception("❌ BITRIX_WEBHOOK не задан в переменных окружения!")

# ============================================================
# КЭШ
# ============================================================
def load_cache():
    """Загружает кэш из файла"""
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}

def save_cache(cache):
    """Сохраняет кэш в файл"""
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"   ⚠️ Ошибка сохранения кэша: {e}")

def is_cache_valid(cached_entry, current_address):
    """
    Проверяет, актуален ли кэш для данного адреса
    - Совпадает ли адрес
    - Не устарел ли по времени
    """
    if not cached_entry:
        return False
    
    # Проверяем, совпадает ли адрес
    if cached_entry.get('address') != current_address:
        return False
    
    # Проверяем, не устарел ли кэш
    timestamp = cached_entry.get('timestamp', 0)
    if time.time() - timestamp > CACHE_TTL:
        return False
    
    # Проверяем, есть ли координаты
    coords = cached_entry.get('coords')
    if not coords:
        return False
    
    return True

# ============================================================
# НОРМАЛИЗАЦИЯ АДРЕСА
# ============================================================
def normalize_address(address):
    """
    Минимальная очистка адреса:
    - Убираем звёздочки и решётки
    - Убираем лишние переносы строк
    - ВСЁ ОСТАЛЬНОЕ СОХРАНЯЕМ для геокодера
    """
    if not address:
        return ''
    
    text = str(address).strip()
    
    # 1. Убираем только мусорные символы
    text = text.replace('*', '').replace('#', '')
    text = text.replace('\n', ' ').replace('\r', ' ')
    
    # 2. Убираем только явный мусор в скобках
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'\[[^\]]*\]', '', text)
    
    # 3. Убираем "Адрес:" в начале
    text = re.sub(r'^Адрес\s*:?\s*', '', text, flags=re.IGNORECASE)
    
    # 4. Убираем "г." в начале (НО сохраняем город!)
    text = re.sub(r'^\s*г\.\s*', '', text, flags=re.IGNORECASE)
    
    # 5. Убираем ЖК, МЦД, МЦК, Метро (только если они отдельно)
    text = re.sub(r'\b(ЖК|МЦД|МЦК|Метро)\s*[«"][^»"]*[»"]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\b(ЖК|МЦД|МЦК|Метро)\s+[А-Яа-яёЁA-Za-z]+\s*', '', text, flags=re.IGNORECASE)
    
    # 6. Заменяем ТОЛЬКО ОЧЕВИДНЫЕ сокращения
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
    }
    
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    
    # 7. Формат "23к7" -> "23 корпус 7"
    text = re.sub(r'(\d+)к(\d+)', r'\1 корпус \2', text, flags=re.IGNORECASE)
    
    # 8. Убираем ТОЛЬКО явные пояснения в КОНЦЕ (не трогаем корпуса!)
    text = re.sub(r',?\s*код\s*домофона[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*домофон[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*ключ[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*парковка[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*Wi-Fi[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*Важно[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*обязательно[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*геолокация[\s\S]*$', '', text, flags=re.IGNORECASE)
    
    # 9. Чистим лишние пробелы и запятые
    text = re.sub(r',+', ',', text)
    text = re.sub(r'\s+', ' ', text)
    text = text.strip().rstrip(',').rstrip('.')
    
    if len(text) < 5:
        return text
    
    # 10. Добавляем "Москва" только если нет города
    cities_pattern = r'(Москва|Санкт-Петербург|Краснодар|Ялта|Сочи|Казань|Екатеринбург|Новосибирск|Мытищи|Видное|Люберцы|Химки|Долгопрудный|Ступино|Котельники|Красногорск|область|край|республика|район|поселок|деревня|село|город)'
    if not re.search(cities_pattern, text, re.IGNORECASE):
        text = 'Москва, ' + text
    
    return text

# ============================================================
# ГЕОКОДИРОВАНИЕ
# ============================================================
def geocode_yandex(address):
    """Геокодирование через Яндекс.Карты"""
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
                # Проверяем, что координаты не в центре Москвы (костыль)
                if abs(lat - 55.7558) > 0.01 or abs(lon - 37.6173) > 0.01:
                    return {'lat': lat, 'lon': lon}
        return None
    except Exception as e:
        return None

def geocode_osm(address):
    """Геокодирование через OpenStreetMap (бесплатно, но медленнее)"""
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
            # Проверяем, что координаты не в центре Москвы (костыль)
            if abs(lat - 55.7558) > 0.01 or abs(lon - 37.6173) > 0.01:
                return {'lat': lat, 'lon': lon}
        return None
    except Exception as e:
        return None

def geocode_address_with_cache(item, cache):
    """
    Геокодирует адрес с использованием кэша по ID записи
    Возвращает: (координаты, очищенный_адрес)
    """
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
    
    # Геокодируем (сначала Яндекс, потом OSM)
    coords = geocode_yandex(clean)
    if not coords:
        coords = geocode_osm(clean)
    
    # Сохраняем в кэш (даже если координат нет - чтобы не геокодить каждый раз)
    cache[cache_key] = {
        'address': address,
        'address_clean': clean,
        'coords': coords,
        'timestamp': time.time()
    }
    save_cache(cache)
    
    return coords, clean

# ============================================================
# ПАРАЛЛЕЛЬНАЯ ОБРАБОТКА
# ============================================================
def process_item(item, cache):
    """Обрабатывает один элемент: извлекает адрес, геокодирует"""
    item_id = item.get('id')
    address = item.get(ADDRESS_FIELD, '')
    stage_id = item.get('stageId', '')
    
    # Геокодируем
    coords, clean = geocode_address_with_cache(item, cache)
    
    return {
        'id': item_id,
        'title': item.get('title', ''),
        'address': address,
        'address_clean': clean,
        'lat': coords['lat'] if coords else None,
        'lon': coords['lon'] if coords else None,
        'stage_id': stage_id,
        'stage_name': item.get('stage_name', ''),
        'cached': cache.get(f"item_{item_id}") is not None  # Отметка, взято из кэша или нет
    }, coords is not None

def process_parallel(items, cache):
    """Обрабатывает элементы параллельно"""
    results = []
    geocoded = 0
    from_cache = 0
    total = len(items)
    start_time = time.time()
    
    print(f"   Запуск {MAX_WORKERS} параллельных потоков...")
    
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
                if result.get('cached'):
                    from_cache += 1
                
                completed += 1
                if completed % BATCH_SIZE == 0 or completed == total:
                    elapsed = time.time() - start_time
                    print(f"   Обработано: {completed}/{total} | Найдено: {geocoded} | Из кэша: {from_cache} | Время: {elapsed:.0f}с")
                    
            except Exception as e:
                print(f"   ❌ Ошибка при обработке элемента: {e}")
                completed += 1
    
    return results, geocoded, from_cache

# ============================================================
# ЗАПРОС К БИТРИКС24
# ============================================================
def fetch_from_bitrix():
    """Загружает все элементы из Битрикс24"""
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
            print(f"   ❌ Ошибка при загрузке: {e}")
            break
    
    return all_items

# ============================================================
# СТАТИСТИКА
# ============================================================
def print_statistics(results, geocoded, from_cache, total):
    """Выводит статистику по обработанным элементам"""
    print("\n" + "="*60)
    print("📊 СТАТИСТИКА ОБРАБОТКИ")
    print("="*60)
    print(f"   Всего элементов:        {total}")
    print(f"   С координатами:         {geocoded} ({geocoded/total*100:.1f}%)")
    print(f"   Без координат:          {total - geocoded} ({(total-geocoded)/total*100:.1f}%)")
    print(f"   Из кэша:                {from_cache} ({from_cache/total*100:.1f}%)")
    print(f"   Новых запросов к API:   {total - from_cache} ({(total-from_cache)/total*100:.1f}%)")
    
    # Статистика по стадиям
    stages = {}
    for r in results:
        stage = r.get('stage_id', 'unknown')
        if stage not in stages:
            stages[stage] = {'total': 0, 'geocoded': 0}
        stages[stage]['total'] += 1
        if r.get('lat'):
            stages[stage]['geocoded'] += 1
    
    print("\n   📋 По стадиям:")
    for stage, data in sorted(stages.items(), key=lambda x: x[1]['total'], reverse=True)[:10]:
        stage_name = data['total']
        geocoded_count = data['geocoded']
        print(f"      {stage}: {geocoded_count}/{data['total']} ({geocoded_count/data['total']*100:.1f}%)")

# ============================================================
# ОСНОВНАЯ ФУНКЦИЯ
# ============================================================
def main():
    print(f"🔄 Обновление данных: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   Яндекс ключ: {'✅ Есть' if YANDEX_API_KEY else '❌ Нет'}")
    print(f"   Время жизни кэша: {CACHE_TTL // 86400} дней")
    print(f"   ⚠️  Режим: обработка ВСЕХ элементов с проверкой актуальности")
    
    # Загружаем кэш
    cache = load_cache()
    cache_size = len(cache)
    print(f"   Кэш: {cache_size} записей")
    
    # Загружаем данные из Битрикс24
    items = fetch_from_bitrix()
    if not items:
        print("❌ Нет данных из Битрикс24")
        return
    
    total = len(items)
    print(f"   Всего элементов для обработки: {total}")
    
    # Обрабатываем
    print(f"\n📍 Геокодирование...")
    results, geocoded, from_cache = process_parallel(items, cache)
    
    # Сохраняем кэш
    save_cache(cache)
    
    # Сохраняем результаты
    output_file = 'data/addresses.json'
    try:
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump({
                'updated_at': datetime.now().isoformat(),
                'total': len(results),
                'geocoded': geocoded,
                'from_cache': from_cache,
                'cache_size': len(cache),
                'items': results
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"   ❌ Ошибка сохранения результатов: {e}")
    
    # Выводим статистику
    print_statistics(results, geocoded, from_cache, total)
    
    print(f"\n✅ Готово! Файл: {output_file}")
    print("="*60)

if __name__ == '__main__':
    main()
