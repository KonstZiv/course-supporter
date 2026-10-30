# DD-SP-CY probe summary

Cells: failed checks / all checks.

## Storage mode x data kind x version

| storage | kind | 17.7 | 17.8 | pgvector-0.8.1-pg17 | 17.9 | 17.10 | 17.11 |
|---|---|---|---|---|---|---|---|
| EXTENDED | uk_prose | 0/420 | **48/420** | **48/420** | 0/420 | 0/420 | 0/420 |
| EXTENDED | uk_code | 0/420 | **54/420** | **54/420** | 0/420 | 0/420 | 0/420 |
| EXTENDED | ascii | 0/420 | 0/420 | 0/420 | 0/420 | 0/420 | 0/420 |
| EXTENDED | mb34 | 0/420 | **54/420** | **54/420** | 0/420 | 0/420 | 0/420 |
| EXTERNAL | uk_prose | 0/420 | **48/420** | **48/420** | 0/420 | 0/420 | 0/420 |
| EXTERNAL | uk_code | 0/420 | **54/420** | **54/420** | 0/420 | 0/420 | 0/420 |
| EXTERNAL | ascii | 0/420 | 0/420 | 0/420 | 0/420 | 0/420 | 0/420 |
| EXTERNAL | mb34 | 0/420 | **54/420** | **54/420** | 0/420 | 0/420 | 0/420 |
| MAIN | uk_prose | 0/420 | **48/420** | **48/420** | 0/420 | 0/420 | 0/420 |
| MAIN | uk_code | 0/420 | **54/420** | **54/420** | 0/420 | 0/420 | 0/420 |
| MAIN | ascii | 0/420 | 0/420 | 0/420 | 0/420 | 0/420 | 0/420 |
| MAIN | mb34 | 0/420 | **51/420** | **51/420** | 0/420 | 0/420 | 0/420 |

## By form

| form | 17.7 | 17.8 | pgvector-0.8.1-pg17 | 17.9 | 17.10 | 17.11 |
|---|---|---|---|---|---|---|
| inline-plain | 0/2121 | 0/2121 | 0/2121 | 0/2121 | 0/2121 | 0/2121 |
| inline-pglz | 0/546 | **111/546** | **111/546** | 0/546 | 0/546 | 0/546 |
| external-plain | 0/1323 | **159/1323** | **159/1323** | 0/1323 | 0/1323 | 0/1323 |
| external-pglz | 0/126 | **39/126** | **39/126** | 0/126 | 0/126 | 0/126 |
| inline-lz4 | 0/609 | **114/609** | **114/609** | 0/609 | 0/609 | 0/609 |
| external-lz4 | 0/315 | **42/315** | **42/315** | 0/315 | 0/315 | 0/315 |

## By compression_setting

| compression_setting | 17.7 | 17.8 | pgvector-0.8.1-pg17 | 17.9 | 17.10 | 17.11 |
|---|---|---|---|---|---|---|
| pglz | 0/2520 | **231/2520** | **231/2520** | 0/2520 | 0/2520 | 0/2520 |
| lz4 | 0/2520 | **234/2520** | **234/2520** | 0/2520 | 0/2520 | 0/2520 |

## By size_label

| size_label | 17.7 | 17.8 | pgvector-0.8.1-pg17 | 17.9 | 17.10 | 17.11 |
|---|---|---|---|---|---|---|
| 500B | 0/1008 | 0/1008 | 0/1008 | 0/1008 | 0/1008 | 0/1008 |
| 1.5KB | 0/1008 | 0/1008 | 0/1008 | 0/1008 | 0/1008 | 0/1008 |
| 3KB | 0/1008 | **105/1008** | **105/1008** | 0/1008 | 0/1008 | 0/1008 |
| 10KB | 0/1008 | **162/1008** | **162/1008** | 0/1008 | 0/1008 | 0/1008 |
| 100KB | 0/1008 | **198/1008** | **198/1008** | 0/1008 | 0/1008 | 0/1008 |

## By query

| query | 17.7 | 17.8 | pgvector-0.8.1-pg17 | 17.9 | 17.10 | 17.11 |
|---|---|---|---|---|---|---|
| left | 0/1200 | **155/1200** | **155/1200** | 0/1200 | 0/1200 | 0/1200 |
| substring_from_for | 0/1200 | **155/1200** | **155/1200** | 0/1200 | 0/1200 | 0/1200 |
| substr | 0/1200 | **155/1200** | **155/1200** | 0/1200 | 0/1200 | 0/1200 |
| right | 0/1200 | 0/1200 | 0/1200 | 0/1200 | 0/1200 | 0/1200 |
| left_concat_400 | 0/240 | 0/240 | 0/240 | 0/240 | 0/240 | 0/240 |

## By n

| n | 17.7 | 17.8 | pgvector-0.8.1-pg17 | 17.9 | 17.10 | 17.11 |
|---|---|---|---|---|---|---|
| 1 | 0/960 | 0/960 | 0/960 | 0/960 | 0/960 | 0/960 |
| 10 | 0/960 | **87/960** | **87/960** | 0/960 | 0/960 | 0/960 |
| 100 | 0/960 | **144/960** | **144/960** | 0/960 | 0/960 | 0/960 |
| 400 | 0/1200 | **162/1200** | **162/1200** | 0/1200 | 0/1200 | 0/1200 |
| 1000 | 0/960 | **72/960** | **72/960** | 0/960 | 0/960 | 0/960 |

## Error messages

- 17.7: no errors, no mismatches
- 17.8: 198 x `22021` invalid byte sequence for encoding "UTF8": 0xd0
- 17.8: 108 x `22021` invalid byte sequence for encoding "UTF8": 0xd1
- 17.8: 54 x `22021` invalid byte sequence for encoding "UTF8": 0xf0
- 17.8: 54 x `22021` invalid byte sequence for encoding "UTF8": 0xf0 0x9f 0x9a
- 17.8: 51 x `22021` invalid byte sequence for encoding "UTF8": 0xe2
- pgvector-0.8.1-pg17: 198 x `22021` invalid byte sequence for encoding "UTF8": 0xd0
- pgvector-0.8.1-pg17: 108 x `22021` invalid byte sequence for encoding "UTF8": 0xd1
- pgvector-0.8.1-pg17: 54 x `22021` invalid byte sequence for encoding "UTF8": 0xf0
- pgvector-0.8.1-pg17: 54 x `22021` invalid byte sequence for encoding "UTF8": 0xf0 0x9f 0x9a
- pgvector-0.8.1-pg17: 51 x `22021` invalid byte sequence for encoding "UTF8": 0xe2
- 17.9: no errors, no mismatches
- 17.10: no errors, no mismatches
- 17.11: no errors, no mismatches

## Mechanism check (slice cut inside a multibyte char)

- 17.7: predicted&failed=0, predicted&ok=465, not-predicted&failed=0, not-predicted&ok=4575
- 17.8: predicted&failed=465, predicted&ok=0, not-predicted&failed=0, not-predicted&ok=4575
- pgvector-0.8.1-pg17: predicted&failed=465, predicted&ok=0, not-predicted&failed=0, not-predicted&ok=4575
- 17.9: predicted&failed=0, predicted&ok=465, not-predicted&failed=0, not-predicted&ok=4575
- 17.10: predicted&failed=0, predicted&ok=465, not-predicted&failed=0, not-predicted&ok=4575
- 17.11: predicted&failed=0, predicted&ok=465, not-predicted&failed=0, not-predicted&ok=4575
