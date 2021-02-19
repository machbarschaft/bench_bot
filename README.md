# README

## Data structure

The data structure to store location information in Google Firestore is as follows:

```yaml
benches:
  <bench_id> (Chat ID - string):
    display_name: <chat title>
    locations:
      <id> (Automatic generation):
        date: <date>
        location:
          <latitude> (number)
          <longitude> (number)
```

## Telegram Bot

### Development

username: bench_dev_bot
name: Machbarschaft - draag_bank - Development
Allow Groups: enabled
Group privacy: disabled
