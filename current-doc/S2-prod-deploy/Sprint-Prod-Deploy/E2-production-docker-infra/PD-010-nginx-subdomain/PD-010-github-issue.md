# PD-010: Nginx Config для subdomain

## Що

Додати server block для `api.pythoncourse.me` до існуючого nginx.conf. Upstream через `shared-net`.

## Навіщо

Nginx вже обслуговує `pythoncourse.me`. Новий subdomain для API — окремий server block з timeouts для великих uploads та security headers.

## Ключові рішення

- Окремий `server` block по `server_name api.pythoncourse.me`
- `client_max_body_size 1G`, `proxy_request_buffering off`
- Timeouts 900s для великих uploads
- Security headers (X-Content-Type-Options, X-Frame-Options)
- Netdata на `/netdata/` з basic auth
- ACME challenge для certbot

## Acceptance Criteria

- [ ] `api.pythoncourse.me` проксюється до app container
- [ ] Upload 1GB не timeout-ає
- [ ] Security headers present
- [ ] HTTP → HTTPS redirect
- [ ] Існуючий `pythoncourse.me` не зачіпається
