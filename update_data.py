import requests
import json
import re
import hashlib
import time
import os
from datetime import datetime

# ============================================================
# КОНФИГУРАЦИЯ (из секретов GitHub)
# ============================================================
BITRIX_WEBHOOK = os.environ.get('BITRIX_WEBHOOK')
ENTITY_TYPE_ID = 1038
ADDRESS_FIELD = 'ufCrm8FullAdress'
YANDEX_API_KEY = os.environ.get('YANDEX_API_KEY', '')

# Проверяем наличие ключей
if not BITRIX_WEBHOOK:
    raise Exception("❌ BITRIX_WEBHOOK не задан в переменных окружения!")

# ============================================================
# КЭШ (храним в репозитории)
# ============================================================
CACHE_FILE = 'data/geocode_cache.json'

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
# НОРМАЛИЗАЦИЯ АДРЕСА
# ============================================================
def normalize_address(address):
    """
    Очищает адрес от лишней информации, оставляя только:
    - Город
    - Улица/проспект/бульвар и т.д.
    - Номер дома (с корпусом, строением)
    """
    if not address:
        return ''
    
    text = str(address).strip()
    
    # 1. Убираем звёздочки, решётки, лишние символы
    text = text.replace('*', '').replace('#', '')
    text = text.replace('\n', ' ').replace('\r', ' ')
    
    # 2. Убираем скобки и их содержимое
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'\[[^\]]*\]', '', text)
    
    # 3. Убираем "г.", "Адрес:" в начале
    text = re.sub(r'^г\.\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^Адрес\s*:?\s*', '', text, flags=re.IGNORECASE)
    
    # 4. Убираем ЖК, МЦД, МЦК, Метро с названиями
    text = re.sub(r'(ЖК|МЦД|МЦК|Метро)\s*[«"][^»"]*[»"]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'(ЖК|МЦД|МЦК|Метро)\s+[А-Яа-яёЁA-Za-z]+\s*', '', text, flags=re.IGNORECASE)
    
    # 5. Заменяем сокращения на полные слова (только целые слова!)
    replacements = {
        r'\bул\.\b': 'улица',
        r'\bпр-д\b': 'проезд',
        r'\bпр-кт\b': 'проспект',
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
    
    # 6. Формат "23к7" -> "23 корпус 7" (только когда цифра+к+цифра)
    text = re.sub(r'(\d+)к(\d+)', r'\1 корпус \2', text, flags=re.IGNORECASE)
    
    # 7. Убираем детали (подъезд, этаж, квартира и т.д.)
    patterns_to_remove = [
        r'подъезд\s*\d+[А-Яа-яёЁ]?',
        r'под\.?\s*\d+[А-Яа-яёЁ]?',
        r'п\.\s*\d+[А-Яа-яёЁ]?',
        r'этаж\s*\d+[А-Яа-яёЁ]?',
        r'эт\.?\s*\d+[А-Яа-яёЁ]?',
        r'эт\s*\d+[А-Яа-яёЁ]?',
        r'кв\.\s*[\d/А-Яа-яёЁ]+',
        r'квартира\s*[\d/А-Яа-яёЁ]+',
        r'апарт\.?\s*[\d/А-Яа-яёЁ]+',
        r'апартаменты\s*[\d/А-Яа-яёЁ]+',
        r'пом\.\s*[\d/А-Яа-яёЁ]+',
        r'помещение\s*[\d/А-Яа-яёЁ]+',
        r'студия\s*[\d/А-Яа-яёЁ]+',
        r'ком\.\s*[\d/А-Яа-яёЁ]+',
        r'комната\s*[\d/А-Яа-яёЁ]+',
        r'секция\s*\d+',
        r'парадная\s*\d+',
    ]
    
    for pattern in patterns_to_remove:
        text = re.sub(r',?\s*' + pattern, '', text, flags=re.IGNORECASE)
    
    # 8. Убираем пояснения в конце
    text = re.sub(r',?\s*код\s*домофона[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*домофон[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*ключ[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*дверь[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*вход[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*парковка[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*Wi-Fi[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*Важно[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*обязательно[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r',?\s*геолокация[\s\S]*$', '', text, flags=re.IGNORECASE)
    
    # 9. Чистим лишние запятые и пробелы
    text = re.sub(r',+', ',', text)
    text = re.sub(r'\s+', ' ', text)
    text = text.strip().rstrip(',').rstrip('.')
    
    # 10. Если адрес слишком короткий — возвращаем как есть
    if len(text) < 5:
        return text
    
    # 11. Добавляем Москву если нет города
    if not re.search(r'(Москва|Санкт-Петербург|Краснодар|Ялта|Сочи|Казань|Екатеринбург|Новосибирск|Мытищи|Видное|Люберцы|Химки|Долгопрудный|Ступино|Котельники|Красногорск|область|край|республика|район|поселок|деревня|село|город)', text, re.IGNORECASE):
        text = 'Москва, ' + text
    
    return text

# ============================================================
# ГЕОКОДИРОВАНИЕ (Яндекс + OSM)
# ============================================================
def geocode_address(address, cache):
    if not address:
        return None
    
    cache_key = hashlib.md5(address.encode()).hexdigest()
    
    # Проверяем кэш
    if cache_key in cache and cache[cache_key]:
        return cache[cache_key]
    
    # Получаем варианты адреса
    variants = get_address_variants(address)
    
    # Пробуем геокодеры по очереди
    geocoders = []
    
    # 1. Яндекс (если есть ключ)
    if YANDEX_API_KEY:
        geocoders.append(('yandex', geocode_yandex))
    
    # 2. OpenStreetMap Nominatim (бесплатно, без ключа)
    geocoders.append(('osm', geocode_osm))
    
    for name, geocoder_func in geocoders:
        for addr in variants:
            try:
                coords = geocoder_func(addr)
                if coords:
                    # Проверяем, что это не центр Москвы (значит нашли точный адрес)
                    if abs(coords['lat'] - 55.7558) > 0.01 or abs(coords['lon'] - 37.6173) > 0.01:
                        cache[cache_key] = coords
                        save_cache(cache)
                        print(f"   ✅ Геокодировано ({name}): {addr[:50]}...")
                        return coords
            except Exception as e:
                print(f"   ⚠️ Ошибка {name}: {e}")
                continue
            
            # Задержка между запросами (для OSM)
            if name == 'osm':
                time.sleep(1)
    
    # Если ничего не нашли
    cache[cache_key] = None
    save_cache(cache)
    return None

def get_address_variants(address):
    """Генерирует варианты адреса для лучшего поиска"""
    variants = [address]
    
    # Убираем "корпус" -> "к"
    if 'корпус' in address:
        variants.append(re.sub(r'корпус\s*(\d+)', r'к\1', address))
    
    # Убираем "строение" -> "стр"
    if 'строение' in address:
        variants.append(re.sub(r'строение\s*(\d+)', r'стр\1', address))
    
    # Только улица и дом (без города)
    parts = address.split(',')
    if len(parts) > 1:
        street_house = parts[0] + ',' + parts[1] if len(parts) > 1 else parts[0]
        variants.append(street_house.strip())
    
    # Без города
    for city in ['Москва, ', 'Санкт-Петербург, ', 'Краснодар, ', 'Ялта, ']:
        if address.startswith(city):
            variants.append(address[len(city):])
            break
    
    # С "Россия" в конце
    if not address.endswith(', Россия'):
        variants.append(address + ', Россия')
    
    # Убираем дубликаты
    return list(dict.fromkeys(variants))

def geocode_yandex(address):
    """Геокодирование через Яндекс (нужен API ключ)"""
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
                return {'lat': float(lat), 'lon': float(lon)}
        
        return None
    except:
        return None

def geocode_osm(address):
    """Геокодирование через OpenStreetMap Nominatim (бесплатно)"""
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
            return {'lat': float(data['lat']), 'lon': float(data['lon'])}
        
        return None
    except:
        return None

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
    print(f"   Вебхук: {BITRIX_WEBHOOK[:50]}...")
    print(f"   Яндекс ключ: {'✅ Есть' if YANDEX_API_KEY else '❌ Нет'}")
    
    # Загружаем кэш
    cache = load_cache()
    print(f"   Кэш: {len(cache)} записей")
    
    # Получаем данные из Битрикса
    items = fetch_from_bitrix()
    if not items:
        print("❌ Нет данных из Битрикс24")
        return
    
    # Обрабатываем
    print("📍 Геокодирование...")
    results = []
    geocoded = 0
    total = len(items)
    
    for i, item in enumerate(items):
        address = item.get(ADDRESS_FIELD, '')
        clean = normalize_address(address) if address else ''
        
        coords = None
        if clean:
            coords = geocode_address(clean, cache)
            if coords:
                geocoded += 1
        
        results.append({
            'id': item.get('id'),
            'title': item.get('title', ''),
            'address': address,
            'address_clean': clean,
            'lat': coords['lat'] if coords else None,
            'lon': coords['lon'] if coords else None,
            'stage_id': item.get('stageId', ''),
            'stage_name': item.get('stage_name', '')
        })
        
        if (i + 1) % 50 == 0 or i == total - 1:
            print(f"   Обработано: {i+1}/{total} | Найдено: {geocoded}")
    
    # Сохраняем результаты
    output_file = 'data/addresses.json'
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump({
            'updated_at': datetime.now().isoformat(),
            'total': len(results),
            'geocoded': geocoded,
            'items': results
        }, f, ensure_ascii=False, indent=2)
    
    print(f"✅ Готово! Всего: {len(results)}, с координатами: {geocoded} ({geocoded/len(results)*100:.0f}%)")
    print(f"   Файл: {output_file}")

if __name__ == '__main__':
    main()
