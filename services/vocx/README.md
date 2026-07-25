# PRISM VocX

Voice-based field touchpoint capture (formerly "VOX"). RMs capture a visit/call —
transcript, GPS, attendees, structured key intel — and VocX writes it to the Register as
an interaction (`source: "VocX"`) through `evam-register-client`, with an idempotency key
so retried uploads never duplicate. Stateless; deployable as an individual component
(own Dockerfile, Compose service, Helm subchart).

Speech-to-text is upstream (device/edge); VocX's contract takes the transcript + intel
already extracted. `POST /v1/touchpoints` → interaction id.
