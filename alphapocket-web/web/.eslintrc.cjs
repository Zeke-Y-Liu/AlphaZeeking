module.exports = {
  root: true,
  env: { browser: true, node: true, es2021: true },
  parser: 'vue-eslint-parser',
  parserOptions: {
    parser: '@typescript-eslint/parser',
    ecmaVersion: 2021,
    sourceType: 'module',
  },
  plugins: ['@typescript-eslint'],
  extends: ['plugin:vue/vue3-essential', 'plugin:@typescript-eslint/recommended'],
  ignorePatterns: [
    'dist/',
    'node_modules/',
    '*.config.ts',
    '*.config.js',
    '.eslintrc.cjs',
    'src/**/*.d.ts',
  ],
  rules: {
    'vue/multi-word-component-names': 'off',
    '@typescript-eslint/no-explicit-any': 'off',
  },
};
