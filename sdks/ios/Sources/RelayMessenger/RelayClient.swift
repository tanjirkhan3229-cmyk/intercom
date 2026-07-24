import Foundation

/// Typed errors surfaced by the SDK. `.http` carries the server's stable error `code`
/// (from the `{ "error": { code, message, ... } }` envelope) when present.
public enum RelayError: Error, LocalizedError {
    case notBooted
    case invalidURL
    case network(Error)
    case decoding(Error)
    case http(status: Int, code: String?, message: String?)

    public var errorDescription: String? {
        switch self {
        case .notBooted: return "Relay is not booted. Call Relay.shared.boot(appId:) first."
        case .invalidURL: return "Invalid URL."
        case .network(let e): return "Network error: \(e.localizedDescription)"
        case .decoding(let e): return "Decoding error: \(e.localizedDescription)"
        case .http(let status, let code, let message):
            return message ?? code ?? "HTTP \(status)"
        }
    }
}

/// async/await URLSession client for the Relay widget API. One instance per booted session.
public actor RelayClient {
    private let baseURL: URL
    private let session: URLSession
    // ponytail: in-memory bearer token only. For session resume across cold launches,
    // persist `sessionToken` to the Keychain (kSecClassGenericPassword, thisDeviceOnly) and
    // restore it here before the first call. Deliberately out of beta scope.
    private var token: String?
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder

    public init(baseURL: URL, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
        let enc = JSONEncoder()
        enc.keyEncodingStrategy = .convertToSnakeCase
        enc.dateEncodingStrategy = .iso8601
        self.encoder = enc
        let dec = JSONDecoder()
        dec.keyDecodingStrategy = .convertFromSnakeCase
        dec.dateDecodingStrategy = .custom { d in
            let s = try d.singleValueContainer().decode(String.self)
            guard let date = RelayDate.parse(s) else {
                throw DecodingError.dataCorrupted(.init(codingPath: d.codingPath,
                                                         debugDescription: "Invalid ISO8601 date: \(s)"))
            }
            return date
        }
        self.decoder = dec
    }

    func setToken(_ t: String?) { token = t }

    // MARK: - Endpoints

    func boot(_ req: BootRequest) async throws -> BootResponse {
        try await perform("POST", "/v0/widget/boot", bodyData: try encoder.encode(req), auth: false)
    }

    func conversations(cursor: String?, limit: Int?) async throws -> Page<Conversation> {
        try await perform("GET", "/v0/widget/conversations",
                          query: items(["cursor": cursor, "limit": limit.map(String.init)]))
    }

    func createConversation(body: String, attachments: [Attachment]?) async throws -> Conversation {
        try await perform("POST", "/v0/widget/conversations",
                          bodyData: try encoder.encode(MessageRequest(body: body, attachments: attachments)))
    }

    func parts(conversationId: String, cursor: String?, after: String?, limit: Int?) async throws -> Page<Part> {
        try await perform("GET", "/v0/widget/conversations/\(conversationId)/parts",
                          query: items(["cursor": cursor, "after": after, "limit": limit.map(String.init)]))
    }

    func reply(conversationId: String, body: String, attachments: [Attachment]?,
               idempotencyKey: String = UUID().uuidString) async throws -> Part {
        try await perform("POST", "/v0/widget/conversations/\(conversationId)/reply",
                          bodyData: try encoder.encode(MessageRequest(body: body, attachments: attachments)),
                          idempotencyKey: idempotencyKey)
    }

    func rate(conversationId: String, rating: Int, remark: String?) async throws -> Part {
        try await perform("POST", "/v0/widget/conversations/\(conversationId)/rating",
                          bodyData: try encoder.encode(RatingRequest(rating: rating, remark: remark)))
    }

    func registerDevice(_ reg: DeviceRegister) async throws -> DeviceRegisterResponse {
        try await perform("POST", "/v0/widget/devices", bodyData: try encoder.encode(reg))
    }

    func unregisterDevice(token: String) async throws {
        try await performVoid("DELETE", "/v0/widget/devices", query: items(["token": token]))
    }

    func presign(filename: String, contentType: String) async throws -> PresignResponse {
        try await perform("POST", "/v0/widget/uploads/presign",
                          bodyData: try encoder.encode(PresignRequest(filename: filename, contentType: contentType)))
    }

    /// Raw PUT of file bytes to a presigned S3 URL. No Authorization header — the URL is signed.
    func putFile(to urlString: String, data: Data, contentType: String) async throws {
        guard let url = URL(string: urlString) else { throw RelayError.invalidURL }
        var req = URLRequest(url: url)
        req.httpMethod = "PUT"
        req.setValue(contentType, forHTTPHeaderField: "Content-Type")
        req.httpBody = data
        let (respData, http) = try await run(req)
        try Self.checkStatus(http, respData)
    }

    // MARK: - Plumbing

    private func items(_ dict: [String: String?]) -> [URLQueryItem] {
        dict.sorted { $0.key < $1.key }.compactMap { k, v in v.map { URLQueryItem(name: k, value: $0) } }
    }

    private func makeRequest(_ method: String, _ path: String, query: [URLQueryItem],
                             bodyData: Data?, idempotencyKey: String?, auth: Bool) throws -> URLRequest {
        guard var comps = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else { throw RelayError.invalidURL }
        var base = comps.path
        if base.hasSuffix("/") { base.removeLast() }
        comps.path = base + path
        if !query.isEmpty { comps.queryItems = query }
        guard let url = comps.url else { throw RelayError.invalidURL }

        var req = URLRequest(url: url)
        req.httpMethod = method
        req.timeoutInterval = 30
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        if let bodyData {
            req.httpBody = bodyData
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        if let idempotencyKey {
            req.setValue(idempotencyKey, forHTTPHeaderField: "Idempotency-Key")
        }
        if auth {
            guard let token else { throw RelayError.notBooted }
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return req
    }

    private func run(_ req: URLRequest) async throws -> (Data, HTTPURLResponse) {
        do {
            let (data, resp) = try await session.data(for: req)
            guard let http = resp as? HTTPURLResponse else {
                throw RelayError.network(URLError(.badServerResponse))
            }
            return (data, http)
        } catch let e as RelayError {
            throw e
        } catch {
            throw RelayError.network(error)
        }
    }

    private func perform<T: Decodable>(_ method: String, _ path: String, query: [URLQueryItem] = [],
                                       bodyData: Data? = nil, idempotencyKey: String? = nil,
                                       auth: Bool = true) async throws -> T {
        let req = try makeRequest(method, path, query: query, bodyData: bodyData,
                                  idempotencyKey: idempotencyKey, auth: auth)
        let (data, http) = try await run(req)
        try Self.checkStatus(http, data)
        do { return try decoder.decode(T.self, from: data) }
        catch { throw RelayError.decoding(error) }
    }

    private func performVoid(_ method: String, _ path: String, query: [URLQueryItem] = [],
                             auth: Bool = true) async throws {
        let req = try makeRequest(method, path, query: query, bodyData: nil, idempotencyKey: nil, auth: auth)
        let (data, http) = try await run(req)
        try Self.checkStatus(http, data)
    }

    private static func checkStatus(_ http: HTTPURLResponse, _ data: Data) throws {
        guard !(200..<300).contains(http.statusCode) else { return }
        let env = try? JSONDecoder().decode(APIErrorEnvelope.self, from: data)
        throw RelayError.http(status: http.statusCode, code: env?.error.code, message: env?.error.message)
    }
}

// Server error envelope: {"error": {code, message, request_id, details}}
private struct APIErrorEnvelope: Decodable {
    struct Inner: Decodable { let code: String?; let message: String? }
    let error: Inner
}

// ISO8601 with and without fractional seconds (Postgres timestamps carry them).
// ponytail: shared formatters — ISO8601DateFormatter parsing is thread-safe in practice.
enum RelayDate {
    private static let fractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]; return f
    }()
    private static let plain: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime]; return f
    }()
    static func parse(_ s: String) -> Date? { fractional.date(from: s) ?? plain.date(from: s) }
}
