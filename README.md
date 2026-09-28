# Ozon Price & Stock Tracker

> **Prototype / supporting project.** This pattern was later reused in the broader [Ozon Seller Bot](https://github.com/d3c0r1x/ozon-seller-bot) and [Smart Shopper](https://github.com/d3c0r1x/smart-shopper) projects.

Telegram bot for monitoring Ozon product prices and stock.

## What it demonstrates

- scheduled polling;
- price/stock change detection;
- personal price thresholds;
- notification cooldowns;
- SQLite persistence;
- retry and graceful handling of temporary marketplace failures.

## Stack

Python · aiogram · Ozon API · SQLite · APScheduler · pytest · GitHub Actions

The repository remains public as a focused example of marketplace monitoring logic.
