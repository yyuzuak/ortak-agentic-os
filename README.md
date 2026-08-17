# Ortak Agentic OS

Farklı kod ajanlarını izole Git worktree'lerinde, insan denetimli hedefler ve
ortak durum üzerinden çalıştırmak için yalın bir şablon repo.

V0.4 ham yapı tamamlandı. Kullanıcı worktree ve model profilini seçer, ajanla
etkileşimli olarak planı olgunlaştırır, hedefi onaylayıp kilitler ve ancak ayrı
bir komutla otonom döngüyü başlatır. Geçici koordinasyon dosya yığını yerine
Git commit'leri ve ignore edilen tek `.agentic/state.sqlite` veritabanı kullanılır.

## Kurulum ve ilk akış

```bash
uv sync
uv run agentic doctor

# Kullanıcı worktree/model seçimini yapar.
uv run agentic worktree create ui --branch task/ui --model default

# Etkileşimli görüşme; bu aşamada otonomi kapalıdır.
uv run agentic chat ui
uv run agentic context ui

# Görüşmede netleşen hedefi kullanıcı açıkça devreye alır.
uv run agentic goal validate goals/demo.yaml
uv run agentic goal approve goals/demo.yaml --worktree ui
uv run agentic goal arm DEMO-001
uv run agentic goal run DEMO-001

# Test ajanı yeni commit'i yalnızca bir kez doğrular.
uv run agentic watch --once

# main'e değil, incelemeye hazır ayrı bir integration branch'ine birleştirir.
uv run agentic goal integrate DEMO-001 --branch integration/sprint-1

# Aynı sprintteki diğer goal'lar aynı branch adıyla aynı integration
# worktree'sine sırayla alınır.
uv run agentic goal integrate OTHER-001 --branch integration/sprint-1
```

`main` dalına son birleşim bilinçli olarak kullanıcıya bırakılır.

## Başka bir projede kullanmak

Runtime, içinde çalıştığı Git reposunu yönetir; bu repoyu klonlamak yerine paket
olarak kurup kendi projene bağlarsın:

```bash
uv add git+https://github.com/yyuzuak/ortak-agentic-os
uv run agentic init
uv run agentic doctor
```

`init` git kökünde `agentic.yaml` ve `goals/example.yaml` oluşturur, `.agentic/`
satırını `.gitignore`'a ekler. Mevcut dosyaları ezmez ve `.gitignore`'u
değiştirmek yerine ona ekler; tekrar çalıştırmak güvenlidir (`--force` ezer).
Git kökü dışında çalıştırılırsa reddeder.

Sonrası aynı: `agentic.yaml` içindeki model profilini kendi ajan CLI'ına
bağlarsın, goal'larını `goals/` altına yazarsın.

## Sürüm katmanları

| Katman | Hazır yetenekler |
|---|---|
| V0.1 | Worktree oluşturma/inceleme/kaldırma, seçilen model profili, tek-yazar lease'i |
| V0.2 | Doğrulanan ve sürümlenen goal; `approve -> arm -> run`; task bağımlılıkları; pause/resume/stop |
| V0.3 | Mock ve genel CLI provider'ı; skills ve loop profilleri; implement-review-test-repair; checkpoint commit'leri |
| V0.4 | Ajanlar arası context görünürlüğü; commit-başına arka plan doğrulama; bütçe kapısı; recovery; güvenli integration branch'i |

Bu katmanlar ayrı ürünler değil, aynı küçük runtime'ın bugünkü V0.4 kapsamına
kadar tamamlanan evrimidir.

## Model bağlama

Runtime model markasına bağlı değildir. `mock` testler içindir. Gerçek bir ajan
CLI'ı `command` provider ile bağlanır; görev paketi JSON olarak stdin'den gelir
ve komut atanmış worktree içinde çalışır:

```yaml
models:
  coder:
    provider: command
    model: your-model-id
    command: ["your-agent", "run", "--stdin-json"]
    interactive_command: ["your-agent"]
```

Claude, Codex, GLM, DeepSeek veya başka bir aracın güncel CLI argümanları bu
profile yazılır; goal, loop ve koordinasyon protokolü değişmez. `skills` ve
`loops` küçük sistemlerde doğrudan `agentic.yaml` içinde tutulabilir.

## Kontrol ve güvenlik sınırları

- Worktree ve model seçimini kullanıcı yapar.
- `chat` yalnızca etkileşimli oturum açar; otonom çalışmayı başlatmaz.
- Onaylanan goal içeriği digest ve sürümle dondurulur.
- Bir worktree'de aynı anda yalnızca bir yazıcı lease alabilir.
- Task ancak review ve kontroller geçince checkpoint commit'i olur.
- Onarım denemesi veya bütçe sınırı aşılırsa çalışma durur.
- Pause/stop istekleri güvenli task sınırlarında uygulanır.
- Ajanlar `context` ile diğer worktree'lerin dal, model, durum ve head bilgisini görür.
- Aynı canlı koordinasyon özeti her otonom task paketine otomatik eklenir.
- `parallel_agents` aynı anda alınabilecek global yazıcı lease sayısını sınırlar;
  kapasite doluysa goal güvenli biçimde pause olur ve daha sonra resume edilebilir.
- Watcher aynı commit'i tekrar test etmez ve kod yazmaz.
- Bir sprintin feature dalları aynı integration worktree'sine ardışık alınabilir.
- Merge conflict veya birleşik test hatası goal'ı durdurur; runtime doğrudan `main`e yazmaz.
- Provider'ın branch değiştirmesi veya runtime dışında commit üretmesi algılanıp goal bloke edilir.
- Kirli worktree sessizce silinmez; dal kaldırma sonrasında korunur.

## Doğrulama ve işletim

```bash
uv run python -W error::ResourceWarning -m unittest discover -s tests -v
uv run agentic events
uv run agentic recover
```

`watch --duration 3600 --interval 30` hafif bir test ajanını belirli süre
çalıştırır. Yarım kalan süreçlerin lease süresi dolduğunda `recover`, koşuyu
devam ettirilebilir `PAUSED` durumuna taşır.

## Repo yüzeyi

```text
AGENTS.md          ortak ve model-bağımsız çalışma kuralları
CLAUDE.md          aynı kuralları Claude uyumlu biçimde içeri alır
agentic.yaml       runtime, model, loop, skill ve doğrulama ayarları
goals/             sürümlenecek hedef tanımları
.agentic/          ignore edilen SQLite, worktree ve integration çalışma alanı
src/agentic_os/    küçük CLI/runtime paketi
tests/             deterministik ve gerçek Git uçtan uca testleri
```

## Bilinçli V0 sınırları

Runtime şu an yereldir; bulut scheduler'ı, web paneli veya dağıtık mesaj kuyruğu
yoktur. Gerçek model CLI komutları kullanıcı ortamına göre profile eklenir.
Watcher deterministik kontrolleri yürütür fakat çalışan ajanın dalına kod yazıp
commit atmaz; düzeltme, ilgili goal'ın kontrollü repair döngüsünde yapılır.
