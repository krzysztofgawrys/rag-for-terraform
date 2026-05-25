export default function hclLanguage(_hljs) {
    const KEYWORDS = {
        keyword: [
            'resource', 'data', 'variable', 'output', 'locals', 'module',
            'provider', 'terraform', 'backend', 'provisioner', 'lifecycle',
            'dynamic', 'content', 'moved', 'import', 'check',
        ],
        literal: ['true', 'false', 'null'],
        built_in: [
            'string', 'number', 'bool', 'list', 'map', 'set', 'object', 'tuple', 'any',
            'for', 'in', 'if', 'each', 'self', 'count', 'depends_on', 'for_each',
            'prevent_destroy', 'create_before_destroy', 'ignore_changes',
        ],
    };
    const STRING = {
        className: 'string',
        variants: [
            { begin: '"', end: '"', contains: [
                    { className: 'subst', begin: '\\$\\{', end: '\\}', contains: [
                            { className: 'variable', begin: '[a-zA-Z_][a-zA-Z0-9_.]*' },
                        ] },
                    { begin: '\\\\[nrt"\\\\]' },
                ] },
            { begin: '<<-?\\s*[A-Z_]+', end: '^\\s*[A-Z_]+', },
        ],
    };
    const NUMBER = {
        className: 'number',
        begin: '\\b\\d+(\\.\\d+)?\\b',
    };
    const COMMENT = {
        className: 'comment',
        variants: [
            { begin: '#', end: '$' },
            { begin: '//', end: '$' },
            { begin: '/\\*', end: '\\*/' },
        ],
    };
    const VARIABLE_REF = {
        className: 'variable',
        begin: '\\b(var|local|module|data|each|self|count|terraform)\\.[a-zA-Z_][a-zA-Z0-9_.]*',
    };
    const FUNCTION_CALL = {
        className: 'title.function',
        begin: '[a-zA-Z_][a-zA-Z0-9_]*\\s*\\(',
        returnBegin: true,
        contains: [
            { className: 'title.function', begin: '[a-zA-Z_][a-zA-Z0-9_]*', },
        ],
    };
    const ATTRIBUTE = {
        className: 'attr',
        begin: '^\\s*[a-zA-Z_][a-zA-Z0-9_-]*\\s*(?==)',
    };
    const BLOCK_TYPE = {
        className: 'keyword',
        begin: '\\b(resource|data|variable|output|locals|module|provider|terraform|backend|provisioner|lifecycle|dynamic|moved|import|check)\\b',
    };
    return {
        name: 'HCL',
        aliases: ['hcl', 'terraform', 'tf'],
        keywords: KEYWORDS,
        contains: [
            COMMENT,
            STRING,
            NUMBER,
            VARIABLE_REF,
            FUNCTION_CALL,
            ATTRIBUTE,
            BLOCK_TYPE,
        ],
    };
}
