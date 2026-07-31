import sqlite3
import datetime

conn = sqlite3.connect("/opt/aimash/db/app.db")
cursor = conn.cursor()

# Mock campaign stats for account 7753643025
# Assuming table 'campaign_stats' with columns: account_id, campaign_id, date, clicks, impressions, spend
account_id = "7753643025"
campaign_id = "demo_camp_1"
date = datetime.datetime.now().strftime("%Y-%m-%d")

cursor.execute(
    """INSERT INTO campaign_stats (account_id, campaign_id, date, clicks, impressions, spend)
                  VALUES (?, ?, ?, ?, ?, ?)""",
    (account_id, campaign_id, date, 20, 1000, 50.0),
)
conn.commit()
conn.close()
print("Warmup data inserted.")
