import SwiftUI

@MainActor
final class ConversationListModel: ObservableObject {
    @Published var conversations: [Conversation] = []
    @Published var theme = RelayTheme()
    @Published var loading = false
    @Published var errorText: String?

    func load() async {
        loading = true
        defer { loading = false }
        theme = await Relay.shared.theme()
        do {
            conversations = try await Relay.shared.conversations().items
            errorText = nil
        } catch {
            errorText = error.localizedDescription
        }
    }

    func start(body: String) async -> Conversation? {
        do {
            let conv = try await Relay.shared.startConversation(body: body)
            conversations.insert(conv, at: 0)
            return conv
        } catch {
            errorText = error.localizedDescription
            return nil
        }
    }
}

/// Native conversation list. Present it directly (e.g. inside a tab or a sheet).
public struct ConversationListView: View {
    @StateObject private var model = ConversationListModel()
    @State private var showCompose = false
    @State private var opened: Conversation?

    public init() {}

    // ponytail: NavigationView (not NavigationStack) to keep the iOS 15 floor. Deprecated on
    // iOS 16+ but still functional; swap for NavigationStack when the floor moves to iOS 16.
    public var body: some View {
        NavigationView {
            content
                .navigationTitle("Messages")
                .toolbar {
                    ToolbarItem(placement: .navigationBarTrailing) {
                        Button { showCompose = true } label: { Image(systemName: "square.and.pencil") }
                    }
                }
                .task { await model.load() }
                .refreshable { await model.load() }
        }
        .navigationViewStyle(.stack)
        .accentColor(model.theme.primaryColor)
        .sheet(isPresented: $showCompose) {
            ComposeView(tint: model.theme.primaryColor) { text in
                showCompose = false
                Task { opened = await model.start(body: text) }
            }
        }
        .sheet(item: $opened) { conv in
            NavigationView { ConversationView(conversationId: conv.id) }
        }
    }

    @ViewBuilder private var content: some View {
        if model.loading && model.conversations.isEmpty {
            ProgressView()
        } else if model.conversations.isEmpty {
            emptyState
        } else {
            List(model.conversations) { conv in
                NavigationLink(destination: ConversationView(conversationId: conv.id)) {
                    row(conv)
                }
            }
            .listStyle(.plain)
        }
    }

    private func row(_ conv: Conversation) -> some View {
        HStack(spacing: 12) {
            Circle()
                .fill(conv.state == "closed" ? Color.secondary.opacity(0.4) : model.theme.primaryColor)
                .frame(width: 8, height: 8)
            VStack(alignment: .leading, spacing: 2) {
                Text("Conversation").font(.body)
                Text(conv.state.capitalized).font(.caption).foregroundColor(.secondary)
            }
            Spacer()
            if let at = conv.lastPartAt {
                Text(at, style: .relative).font(.caption2).foregroundColor(.secondary)
            }
        }
        .padding(.vertical, 4)
    }

    private var emptyState: some View {
        VStack(spacing: 12) {
            Image(systemName: "bubble.left.and.bubble.right")
                .font(.largeTitle).foregroundColor(.secondary)
            Text("No conversations yet").foregroundColor(.secondary)
            Button("Start a conversation") { showCompose = true }
                .buttonStyle(.borderedProminent)
                .tint(model.theme.primaryColor)
            if let err = model.errorText {
                Text(err).font(.caption).foregroundColor(.red).multilineTextAlignment(.center)
            }
        }
        .padding()
    }
}

/// Minimal "new conversation" composer sheet.
struct ComposeView: View {
    let tint: Color
    let onSend: (String) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""

    var body: some View {
        NavigationView {
            VStack {
                TextEditor(text: $text)
                    .frame(minHeight: 120)
                    .padding(8)
                    .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.secondary.opacity(0.3)))
                    .padding()
                Spacer()
            }
            .navigationTitle("New message")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Send") { onSend(text) }
                        .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }
        .accentColor(tint)
    }
}
