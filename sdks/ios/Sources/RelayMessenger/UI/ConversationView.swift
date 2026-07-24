import SwiftUI

@MainActor
final class ConversationThreadModel: ObservableObject {
    let conversationId: String
    @Published var parts: [Part] = []
    @Published var draft = ""
    @Published var theme = RelayTheme()
    @Published var sending = false
    @Published var errorText: String?

    private var lastId: String?
    private var pollTask: Task<Void, Never>?

    init(conversationId: String) { self.conversationId = conversationId }

    func start() async {
        theme = await Relay.shared.theme()
        await loadInitial()
        startPolling()
    }

    func loadInitial() async {
        do {
            let page = try await Relay.shared.parts(conversationId: conversationId)
            apply(page.items)
            errorText = nil
        } catch {
            errorText = error.localizedDescription
        }
    }

    // ponytail: beta transport = polling `parts?after=`. The realtime upgrade is the
    // `/conversations/{id}/realtime-token` endpoint + Centrifugo websocket — intentionally
    // not wired here. Swap `poll()` for a websocket subscription when it ships.
    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                if Task.isCancelled { break }
                await self?.poll()
            }
        }
    }

    private func poll() async {
        guard let lastId else { return await loadInitial() }
        do {
            let page = try await Relay.shared.parts(conversationId: conversationId, after: lastId)
            if !page.items.isEmpty { apply(page.items) }
        } catch {
            // Transient poll failures are swallowed; the next tick retries.
        }
    }

    func stop() {
        pollTask?.cancel()
        pollTask = nil
    }

    func send() async {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !sending else { return }
        sending = true
        defer { sending = false }
        draft = ""
        do {
            let part = try await Relay.shared.reply(conversationId: conversationId, body: text)
            apply([part])
        } catch {
            errorText = error.localizedDescription
            draft = text   // restore so the user can retry
        }
    }

    /// Merge incoming parts by id, keep only contact/admin/ai comments + ratings, sort by time.
    private func apply(_ incoming: [Part]) {
        var byId = Dictionary(parts.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        for p in incoming where p.partType == .comment || p.partType == .rating {
            byId[p.id] = p
        }
        parts = byId.values.sorted { $0.createdAt < $1.createdAt }
        lastId = parts.last?.id
    }
}

/// Native conversation thread: message bubbles + a composer. Presentable standalone (e.g. for a
/// push deep-link) or pushed from `ConversationListView`.
public struct ConversationView: View {
    @StateObject private var model: ConversationThreadModel

    public init(conversationId: String) {
        _model = StateObject(wrappedValue: ConversationThreadModel(conversationId: conversationId))
    }

    public var body: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 8) {
                        ForEach(model.parts) { part in
                            Bubble(part: part, tint: model.theme.primaryColor).id(part.id)
                        }
                    }
                    .padding()
                }
                .onChange(of: model.parts.count) { _ in
                    if let last = model.parts.last {
                        withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                    }
                }
            }
            composer
        }
        .navigationTitle("Conversation")
        .navigationBarTitleDisplayMode(.inline)
        .task { await model.start() }
        .onDisappear { model.stop() }
    }

    private var composer: some View {
        HStack(spacing: 8) {
            // ponytail: single-line TextField keeps the iOS 15 floor (multiline `axis:`
            // TextField is iOS 16+). Send on submit or the button.
            TextField("Message…", text: $model.draft)
                .textFieldStyle(.roundedBorder)
                .submitLabel(.send)
                .onSubmit { Task { await model.send() } }
            Button {
                Task { await model.send() }
            } label: {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.title2)
                    .foregroundColor(canSend ? model.theme.primaryColor : .secondary)
            }
            .disabled(!canSend)
        }
        .padding(8)
        .background(.thinMaterial)
    }

    private var canSend: Bool {
        !model.sending && !model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
}

/// A single message bubble. Contact (the end user) messages are outgoing (right, tinted);
/// admin / AI / system messages are incoming (left, neutral).
struct Bubble: View {
    let part: Part
    let tint: Color

    private var isOutgoing: Bool { part.authorKind == .contact }

    var body: some View {
        HStack {
            if isOutgoing { Spacer(minLength: 40) }
            VStack(alignment: .leading, spacing: 4) {
                if let body = part.body, !body.isEmpty {
                    Text(body)
                }
                ForEach(part.attachments ?? [], id: \.key) { att in
                    Label(att.filename, systemImage: "paperclip").font(.caption)
                }
            }
            .padding(10)
            .foregroundColor(isOutgoing ? .white : .primary)
            .background(isOutgoing ? tint : Color.secondary.opacity(0.15))
            .clipShape(RoundedRectangle(cornerRadius: 14))
            if !isOutgoing { Spacer(minLength: 40) }
        }
        .frame(maxWidth: .infinity, alignment: isOutgoing ? .trailing : .leading)
    }
}
