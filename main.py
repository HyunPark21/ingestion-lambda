from ingestion.fetch_data import collect_all

if __name__ == "__main__":
    data = collect_all()
    print(data)