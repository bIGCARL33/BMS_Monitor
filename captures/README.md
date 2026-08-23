# Captures

Raw byte dumps written by `jkbms sniff`. Feed one back through the whole
pipeline with `--replay`:

```
jkbms probe --replay captures/run1.bin --discover
jkbms log   --replay captures/run1.bin -o run1.csv
```

`.bin` and `.hex` files here are gitignored — they are session data. If a
particular capture is worth keeping as a protocol fixture (a new firmware
revision, an odd frame), force-add it and say in the commit what it shows.
