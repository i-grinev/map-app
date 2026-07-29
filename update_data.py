# ============================================================
# ОСНОВНАЯ ФУНКЦИЯ
# ============================================================
def main():
    print(f"🔄 Обновление данных: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   Яндекс ключ: {'✅ Есть' if YANDEX_API_KEY else '❌ Нет'}")
    print(f"   Принудительных координат: {len(FORCED_COORDINATES)}")
    
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
    results, geocoded, from_cache, forced = process_parallel(items, cache)
    
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
                'forced': forced,
                'cache_size': len(cache),
                'items': results
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"   ❌ Ошибка сохранения результатов: {e}")
    
    # ✅ НОВОЕ: Сохраняем конфиг для фронтенда
    config_file = 'data/config.js'
    try:
        os.makedirs(os.path.dirname(config_file), exist_ok=True)
        with open(config_file, 'w', encoding='utf-8') as f:
            f.write(f"// Автоматически сгенерировано {datetime.now().isoformat()}\n")
            f.write(f"window.YANDEX_API_KEY = '{YANDEX_API_KEY}';\n")
        print(f"   ✅ Конфиг сохранён: {config_file}")
    except Exception as e:
        print(f"   ❌ Ошибка сохранения конфига: {e}")
    
    # Выводим статистику
    print_statistics(results, geocoded, from_cache, forced, total)
    
    print(f"\n✅ Готово! Файл: {output_file}")
    print("="*60)
